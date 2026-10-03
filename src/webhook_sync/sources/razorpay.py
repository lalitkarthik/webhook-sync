"""The Razorpay source adapter (sources.md, "Razorpay: the differences").

Signature: `x-razorpay-signature`, hex HMAC-SHA256 of the raw body, no timestamp, compared through
the SDK (constant time). Event id: the `x-razorpay-event-id` header, which the signature doesn't
cover. Razorpay events carry no mode; the configured mode is used, and the key prefix is checked at
start. Every exception becomes a rejection reason, never a 500.

Fetch: one call, `GET /v1/payments/{id}?expand[]=refunds&expand[]=card`, made directly with
async httpx (basic auth, a 10 s timeout, A11), never the synchronous SDK. When the expanded
refunds fill Razorpay's default page of 10, `GET /v1/payments/{id}/refunds` is paged for the full
list (A6). Every call first takes a token from a 10-a-second bucket, and its answer is classified
by HTTP status (A12): 400 "The id provided does not exist" is `NotFound`; 401 and 403 pause the
source; 429, 5xx, timeouts and connection errors retry, `Retry-After` a minimum; anything else,
or a 2xx that isn't a JSON object, is dead with its reason. `payment_row` maps the answer to a
business row, a pure function (sources.md, "Fetch").
"""

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, cast

import httpx
from razorpay import Utility  # pyright: ignore[reportMissingTypeStubs]
from razorpay.errors import SignatureVerificationError  # pyright: ignore[reportMissingTypeStubs]

from webhook_sync.clock import utc_now
from webhook_sync.config import Mode
from webhook_sync.db.writer import Writer
from webhook_sync.queue import Failure, Pauses, pause
from webhook_sync.receiver.rate_cap import RateCap
from webhook_sync.sources.base import (
    BusinessRow,
    EventDescription,
    Fetched,
    FetchResult,
    NotFound,
    PaymentStatus,
    Rejection,
    UnusableState,
    VerifiedDelivery,
)

SIGNATURE_HEADER = "x-razorpay-signature"
EVENT_ID_HEADER = "x-razorpay-event-id"

# Q109: the types that make a ledger job. Every other type is stored and ignored with a reason.
HANDLED_TYPES = frozenset(
    {
        "payment.authorized",
        "payment.captured",
        "payment.failed",
        "refund.created",
        "refund.processed",
        "refund.failed",
        "refund.speed_changed",
    }
)

# The primary entity, by event-type prefix, most specific first (Q117).
_PRIMARY_ENTITY = (
    ("payment.dispute.", "dispute"),
    ("payment_link.", "payment_link"),
    ("payment.", "payment"),
    ("refund.", "refund"),
    ("order.", "order"),
)

# Q119 / A5: what `events.payload` keeps. Everything else (email, contact, notes, card, VPA, bank
# details, fields Razorpay adds later) is dropped before storing.
_TOP_LEVEL_KEYS = frozenset({"entity", "account_id", "event", "contains", "created_at"})
_ENTITY_FIELDS = frozenset(
    {
        "id",
        "entity",
        "amount",
        "amount_refunded",
        "currency",
        "status",
        "refund_status",
        "captured",
        "order_id",
        "payment_id",
        "method",
        "speed_requested",
        "speed_processed",
        "error_code",
        "error_reason",
        "created_at",
    }
)

# Where an event names its payment, in order (A16): stored for ignored events too.
_PAYMENT_KEY_PATHS = (("payment", "id"), ("refund", "payment_id"), ("dispute", "payment_id"))


FETCH_TIMEOUT = timedelta(seconds=10)
"""How long one fetch may take before it is given up and retried (A11)."""

CALLS_PER_SECOND = 10.0
"""The source's token bucket: at most 10 API calls a second, a burst of 10 (sources.md). A
call waits for a token, so a burst never meets Razorpay's unpublished throttle."""


def api_client(api_url: str, key_id: str, key_secret: str) -> httpx.AsyncClient:
    """The client every Razorpay API call goes through: basic auth with the key, 10 s timeout."""
    return httpx.AsyncClient(
        base_url=api_url,
        auth=httpx.BasicAuth(key_id, key_secret),
        timeout=FETCH_TIMEOUT.total_seconds(),
    )


EXPANDED_PAGE = 10
"""Razorpay's default page size: an expanded refunds list this long may have been cut short, so
the refunds endpoint is paged instead (A6; `count` is per page, never a total)."""

REFUNDS_PAGE = 100
"""Refunds asked for per page of `GET /v1/payments/{id}/refunds`, the most Razorpay allows."""


LIST_PAGE = 100
"""Items asked for per page of a payments or refunds list, the most Razorpay allows."""

FIRST_SECOND = 946684800
LAST_SECOND = 4765046400
"""The Unix seconds Razorpay's payments list accepts as a bound (G71); a millisecond time is
far past the last."""


@dataclass(frozen=True)
class Listed:
    """Every item a list returned for a range, newest first, and Razorpay's time from the
    first page's `Date` header: the sweeper's clock is Razorpay's, never ours (A14)."""

    items: tuple[dict[str, Any], ...]
    source_now: datetime


def _check_bounds(start: int, end: int) -> None:
    """Refuse bounds that aren't Unix seconds, or a `start` after `end`, before any call."""
    for name, bound in (("start", start), ("end", end)):
        if type(bound) is not int:  # a bool or a float slips past the type checker
            raise ValueError(f"a list's {name} must be Unix seconds, not {bound!r}")
        if not FIRST_SECOND <= bound <= LAST_SECOND:
            raise ValueError(f"a list's {name} {bound} is not Unix seconds (milliseconds?)")
    if start > end:
        raise ValueError(f"a list's start {start} is after its end {end}")


def _date(value: str | None) -> datetime | None:
    """An HTTP `Date` header as a UTC time, or None when absent or unreadable."""
    if value is None:
        return None
    try:
        when = parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)


def _list_failure(answer: NotFound | Failure) -> Failure:
    """A list's failure: as the fetch's, except that a list has no id to be unknown."""
    if isinstance(answer, Failure):
        return answer
    return Failure(f"Razorpay answered HTTP {answer.http_status} to a list", "dead")


UNKNOWN_ID = "The id provided does not exist"
"""Razorpay's 400 description for a payment id it doesn't know: not found at source (A5)."""


def _error_description(response: httpx.Response) -> str | None:
    """The `error.description` of a Razorpay error answer, or None when it has none."""
    try:
        body: object = response.json()
    except ValueError:
        return None
    description = _object(_object(body).get("error")).get("description")
    return description if isinstance(description, str) else None


def _retry_after(value: str | None) -> timedelta | None:
    """A `Retry-After` header as a wait: whole seconds, or an HTTP date from now (A12). None
    when absent or unreadable; a date already past is no wait."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return timedelta(seconds=int(value))
    try:
        when = parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)  # an HTTP date is always GMT
    return max(when - utc_now(), timedelta(0))


class RazorpayAdapter:
    name = "razorpay"

    def __init__(
        self,
        *,
        mode: Mode,
        account: str,
        webhook_secrets: Sequence[str],
        api: httpx.AsyncClient,
        metadata_keys: Sequence[str] = (),
        fetch_timeout: timedelta = FETCH_TIMEOUT,
    ) -> None:
        self._fetch_timeout = fetch_timeout
        self._bucket = RateCap(rate=CALLS_PER_SECOND, burst=CALLS_PER_SECOND)
        self._mode = mode
        self._metadata_keys = tuple(metadata_keys)
        self._account = account
        self._secrets = tuple(webhook_secrets)
        self._api = api

    async def fetch(self, payment_key: str) -> FetchResult:
        """The payment's current state, with its refunds and card expanded."""
        state = await self._get(
            f"/v1/payments/{payment_key}", [("expand[]", "refunds"), ("expand[]", "card")]
        )
        if not isinstance(state, dict):
            return state
        expanded = _object(state.get("refunds")).get("items")
        if isinstance(expanded, list) and len(cast(list[object], expanded)) >= EXPANDED_PAGE:
            refunds = await self._all_refunds(payment_key)
            if not isinstance(refunds, list):
                return refunds
            state = {**state, "refunds": {"entity": "collection", "items": refunds}}
        return Fetched(state)

    async def _all_refunds(self, payment_key: str) -> list[dict[str, Any]] | NotFound | Failure:
        """Every refund of the payment, paged 100 at a time until a page comes back short.

        A refunds collection's `count` is the items on that page, not a total, so a short page
        is the only sign of the end (the fetch recordings' README; #22). Each page's answer is
        classified as the main fetch's is."""
        refunds: list[dict[str, Any]] = []
        while True:
            answer = await self._get(
                f"/v1/payments/{payment_key}/refunds",
                [("count", str(REFUNDS_PAGE)), ("skip", str(len(refunds)))],
            )
            if not isinstance(answer, dict):
                return answer
            page = cast(list[dict[str, Any]], answer["items"])
            refunds.extend(page)
            if len(page) < REFUNDS_PAGE:
                return refunds

    async def list_payments(
        self, start: int, end: int, *, writer: Writer, pauses: Pauses
    ) -> Listed | Failure:
        """Every payment created from `start` to `end`, both inclusive, newest first. A 401 or
        403 pauses the source in `sinks` and `pauses`, with the `paused` alert."""
        return await self._paused_on_refusal(
            await self._list("/v1/payments", start, end), writer, pauses
        )

    async def list_refunds(
        self, start: int, end: int, *, writer: Writer, pauses: Pauses
    ) -> Listed | Failure:
        """Every refund created from `start` to `end`, as `list_payments`. The `status` filter
        is never sent: Razorpay accepts it and doesn't filter (G73)."""
        return await self._paused_on_refusal(
            await self._list("/v1/refunds", start, end), writer, pauses
        )

    async def _paused_on_refusal(
        self, answer: Listed | Failure, writer: Writer, pauses: Pauses
    ) -> Listed | Failure:
        """A list runs in no job, so a `pause` failure pauses the source here (A12)."""
        if isinstance(answer, Failure) and answer.error_class == "pause":
            await pause(writer, pauses, "source", self.name, answer.error, now=utc_now())
        return answer

    async def _list(self, path: str, start: int, end: int) -> Listed | Failure:
        """Page a list by creation time (sweeper.md, "Razorpay: polling").

        Both bounds are always sent, in Unix seconds: a missing bound means unbounded, and the
        refunds list reads a millisecond `to` as no bound at all (G71, G73). Each next page
        moves `to` to the oldest `created_at` seen plus one and drops ids already seen; a page
        that can't move `to` down (over 100 items in two seconds) is paged on with `skip`. That
        doesn't rely on the order of items created in the same second. A short page is the
        end: skip past the end is a 200 with no items."""
        _check_bounds(start, end)
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        source_now: datetime | None = None
        upper, skip = end, 0
        while True:
            params = [("from", str(start)), ("to", str(upper)), ("count", str(LIST_PAGE))]
            if skip:
                params.append(("skip", str(skip)))
            answer = await self._call(path, params)
            if not isinstance(answer, tuple):
                return _list_failure(answer)
            body, headers = answer
            if source_now is None:
                source_now = _date(headers.get("date"))
                if source_now is None:
                    return Failure("Razorpay answered a list with no readable Date", "retry")
            page = cast(list[dict[str, Any]], body.get("items") or [])
            items.extend(item for item in page if item["id"] not in seen)
            seen.update(item["id"] for item in page)
            if len(page) < LIST_PAGE:
                return Listed(tuple(items), source_now)
            oldest = min(item["created_at"] for item in page)
            if oldest + 1 < upper:
                upper, skip = oldest + 1, 0
            else:
                skip += len(page)

    async def _get(
        self, path: str, params: Sequence[tuple[str, str]]
    ) -> dict[str, Any] | NotFound | Failure:
        """One API call: the answer's JSON object, or what its failure means (A12)."""
        answer = await self._call(path, params)
        return answer[0] if isinstance(answer, tuple) else answer

    async def _call(
        self, path: str, params: Sequence[tuple[str, str]]
    ) -> tuple[dict[str, Any], httpx.Headers] | NotFound | Failure:
        """One API call, once the bucket gives a token: the answer's JSON object and headers,
        or what its failure means (A12)."""
        # The bucket refills with the one clock's time, so it is polled, a token's time apart.
        while True:
            if self._bucket.allow(self.name):
                break
            await asyncio.sleep(1 / CALLS_PER_SECOND)
        try:
            # The whole call, not only each read, ends at the timeout: a trickle can't hang it.
            async with asyncio.timeout(self._fetch_timeout.total_seconds()):
                response = await self._api.get(path, params=list(params))
        except TimeoutError:
            return Failure("Razorpay call failed: no answer within the timeout", "retry")
        except httpx.TransportError as error:  # a timeout, a refused or dropped connection
            return Failure(f"Razorpay call failed: {type(error).__name__}", "retry")
        status = response.status_code
        if status == 400 and _error_description(response) == UNKNOWN_ID:
            return NotFound(http_status=400)
        if status in (401, 403):
            return Failure(
                f"Razorpay answered HTTP {status}",
                "pause",
                pause_target="source",
                http_status=status,
            )
        if status == 429 or status >= 500:
            return Failure(
                f"Razorpay answered HTTP {status}",
                "retry",
                retry_after=_retry_after(response.headers.get("retry-after")),
                http_status=status,
            )
        if not response.is_success:
            description = _error_description(response)
            reason = f"Razorpay answered HTTP {status}"
            if description:
                reason = f"{reason}: {description[:200]}"
            return Failure(reason, "dead", http_status=status)
        try:
            body: object = response.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return Failure(
                f"Razorpay answered HTTP {status} with a body that is not a JSON object",
                "dead",
                http_status=status,
            )
        return cast(dict[str, Any], body), response.headers

    def to_row(self, state: Mapping[str, Any]) -> BusinessRow | None:
        return payment_row(state, metadata_keys=self._metadata_keys)

    def verify(self, raw_body: bytes, headers: Mapping[str, str]) -> VerifiedDelivery | Rejection:
        try:
            return self._verify(raw_body, headers)
        except Exception:
            return Rejection("bad_signature")

    def describe(self, delivery: VerifiedDelivery) -> EventDescription | Rejection:
        try:
            return self._describe(delivery.body)
        except Exception:
            return Rejection("malformed")

    def _verify(self, raw_body: bytes, headers: Mapping[str, str]) -> VerifiedDelivery | Rejection:
        signature = headers.get(SIGNATURE_HEADER)
        if not signature:
            return Rejection("bad_signature")
        try:
            text = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            return Rejection("malformed")
        if not any(self._signed_with(text, signature, secret) for secret in self._secrets):
            return Rejection("bad_signature")
        try:
            body: object = json.loads(text)
        except ValueError, RecursionError:
            return Rejection("malformed")
        if not _encodes_as_utf8(body) or not isinstance(body, dict):
            return Rejection("malformed")
        event_id = headers.get(EVENT_ID_HEADER, "").strip()
        if not event_id:
            return Rejection("missing_id")
        return VerifiedDelivery(
            event_id=event_id,
            body=cast(dict[str, Any], body),
            body_sha256=hashlib.sha256(raw_body).hexdigest(),
        )

    @staticmethod
    def _signed_with(text: str, signature: str, secret: str) -> bool:
        try:
            Utility().verify_webhook_signature(text, signature, secret)  # pyright: ignore[reportUnknownMemberType]
        except SignatureVerificationError, TypeError, ValueError:
            return False
        return True

    def _describe(self, body: Mapping[str, Any]) -> EventDescription | Rejection:
        event_type = body.get("event")
        if not isinstance(event_type, str) or not event_type:
            return Rejection("malformed")
        # Q121: a body naming another account is refused; one naming none is the configured one's.
        account_id = body.get("account_id")
        if account_id is not None and account_id != self._account:
            return Rejection("wrong_account")
        handled = event_type in HANDLED_TYPES

        entity_name = next(
            (name for prefix, name in _PRIMARY_ENTITY if event_type.startswith(prefix)), None
        )
        entity_id = _entity_field(body, entity_name, "id") if entity_name else None
        payment_key = next(
            (
                key
                for name, field in _PAYMENT_KEY_PATHS
                if (key := _entity_field(body, name, field))
            ),
            None,
        )
        if handled and entity_id is None:
            return Rejection("malformed")
        # A job is keyed by its payment: a handled type that names none is kept, ignored, no job.
        ignore_reason = (
            "unhandled_type" if not handled else "no_payment" if payment_key is None else None
        )

        created_at = body.get("created_at")
        created_seconds = (
            created_at if isinstance(created_at, int) and not isinstance(created_at, bool) else None
        )
        fingerprint_parts = (
            event_type,
            entity_id or "",
            "" if created_seconds is None else str(created_seconds),
            self._mode,
        )
        return EventDescription(
            type=event_type,
            account_id=account_id,
            payment_key=payment_key,
            fingerprint=hashlib.sha256("|".join(fingerprint_parts).encode()).hexdigest(),
            source_created_at=(
                None if created_seconds is None else datetime.fromtimestamp(created_seconds, UTC)
            ),
            ignore_reason=ignore_reason,
            payload=_allow_listed(body),
        )


def _allow_listed(body: Mapping[str, Any]) -> dict[str, Any]:
    """The body with only Q119's fields: allow-listed top-level keys, and under `payload` each
    entity's allow-listed fields. Only plain values are kept, so a nested object never slips in
    under an allowed name."""
    kept: dict[str, Any] = {k: v for k, v in body.items() if k in _TOP_LEVEL_KEYS and _plain(v)}
    entities = {
        name: {"entity": {k: v for k, v in entity.items() if k in _ENTITY_FIELDS and _plain(v)}}
        for name, wrapper in _object(body.get("payload")).items()
        if (entity := _object(_object(wrapper).get("entity")))
    }
    if entities:
        kept["payload"] = entities
    return kept


def _object(value: object) -> dict[str, Any]:
    """`value` when it is a JSON object, else an empty one."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else {}


def _plain(value: object) -> bool:
    """A scalar, or a list of strings (`contains`)."""
    if isinstance(value, list):
        return all(isinstance(item, str) for item in cast(list[object], value))
    return value is None or isinstance(value, str | int | float | bool)


def _encodes_as_utf8(body: object) -> bool:
    """False when a `\\ud800`-style escape left a lone surrogate, which SQLite can't store."""
    try:
        json.dumps(body, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _entity_field(body: Mapping[str, Any], entity_name: str, field: str) -> str | None:
    """`payload.<entity_name>.entity.<field>` when it is a non-empty string, else None."""
    node: object = body.get("payload")
    for key in (entity_name, "entity", field):
        if not isinstance(node, dict):
            return None
        node = cast(dict[str, object], node).get(key)
    return node if isinstance(node, str) and node else None


_STATUSES: dict[str, PaymentStatus] = {
    "authorized": "authorized",
    "captured": "succeeded",
    "failed": "failed",
}


def _status(source_status: str, *, captured: bool) -> PaymentStatus:
    """Our status before refunds are counted: a `refunded` payment was captured (it is then
    refunded by its processed refunds) or was an authorisation auto-refunded uncaptured (A7)."""
    if source_status == "refunded":
        return "succeeded" if captured else "canceled"
    status = _STATUSES.get(source_status)
    if status is None:
        raise UnusableState(f"unmapped Razorpay payment status {source_status!r}")
    return status


def _processed_refunds(state: Mapping[str, Any]) -> int:
    """The sum of the payment's `processed` refunds; pending and failed ones count for nothing.
    `amount_refunded` is never used: whether it counts pending refunds is undocumented (G65)."""
    items = cast(list[dict[str, Any]], _object(state.get("refunds")).get("items") or [])
    return sum(refund["amount"] for refund in items if refund.get("status") == "processed")


# A8 / Q129: the one non-personal detail shown with each method, and where it is read.
_METHOD_DETAIL = {"card": ("card", "network"), "netbanking": ("bank",), "wallet": ("wallet",)}


def _method(state: Mapping[str, Any]) -> str | None:
    """`method` plus its card network, bank code or wallet name, as `card (Visa)`; any other
    method (`upi`, `emi`, `paylater`, ...) alone. Never last4, the card's name or a VPA."""
    method = state.get("method")
    if not isinstance(method, str) or not method:
        return None
    detail: object = state
    for key in _METHOD_DETAIL.get(method, ()):
        detail = _object(detail).get(key)
    return f"{method} ({detail})" if isinstance(detail, str) and detail else method


class BadAmount(UnusableState):
    """The source answered an amount that is negative or not an integer: bad data, never
    written. The ledger worker sends the job to dead with this message (A14, edge case D4)."""


def _amount(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BadAmount(f"bad amount from Razorpay: {value!r} is not a non-negative integer")
    return value


def payment_row(
    state: Mapping[str, Any], *, metadata_keys: Sequence[str] = ()
) -> BusinessRow | None:
    """A fetched Razorpay payment as its business row, or None while it is still `created`.

    A captured payment (or `refunded` with `captured: true`) is succeeded, partially_refunded or
    refunded by its processed refunds against the amount; `refunded` uncaptured is canceled (A7).
    Fee and fee tax (GST) are null until capture: pending. They are in the settlement currency
    (`base_currency`, else `currency`), and net is `(base_amount or amount) - fee` (Q130).
    Metadata is the `metadata_keys` found in `notes`, values as given (Q131). A negative or
    non-integer amount raises `BadAmount`; an unknown currency is written as given (A14).
    """
    source_status: str = state["status"]
    if source_status == "created":
        return None  # nothing attempted yet: no row (G41)
    amount = _amount(state.get("amount"))
    refunded = _processed_refunds(state)
    status = _status(source_status, captured=state.get("captured") is True)
    if status == "succeeded" and refunded > 0:
        status = "refunded" if refunded >= amount else "partially_refunded"
    fee: int | None = state.get("fee")
    notes = _object(state.get("notes"))  # empty notes arrive as `[]` (Q131)
    created_at = state.get("created_at")
    return BusinessRow(
        payment_id=state["id"],
        status=status,
        source_status=source_status,
        amount_minor=amount,
        currency=state["currency"],
        refunded_minor=refunded,
        source_created_at=(
            datetime.fromtimestamp(created_at, UTC) if isinstance(created_at, int) else None
        ),
        fee_minor=fee,
        fee_tax_minor=state.get("tax"),
        net_minor=None if fee is None else (state.get("base_amount") or amount) - fee,
        settlement_currency=state.get("base_currency") or state["currency"],
        method=_method(state),
        metadata={key: notes[key] for key in metadata_keys if key in notes},
        # A4: email and description as reported; never `contact`, a VPA or card details.
        customer_email=state.get("email"),
        description=state.get("description"),
    )
