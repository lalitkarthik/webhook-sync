"""The Razorpay source adapter (sources.md, "Razorpay: the differences").

Signature: `x-razorpay-signature`, hex HMAC-SHA256 of the raw body, no timestamp, compared through
the SDK (constant time). Event id: the `x-razorpay-event-id` header, which the signature doesn't
cover. Razorpay events carry no mode; the configured mode is used, and the key prefix is checked at
start. Every exception becomes a rejection reason, never a 500.

Fetch: one call, `GET /v1/payments/{id}?expand[]=refunds&expand[]=card`, made directly with
async httpx (basic auth, a 10 s timeout, A11), never the synchronous SDK. When the expanded
refunds fill Razorpay's default page of 10, `GET /v1/payments/{id}/refunds` is paged for the full
list (A6). `payment_row` maps the
answer to a business row, a pure function (sources.md, "Fetch").
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
from razorpay import Utility  # pyright: ignore[reportMissingTypeStubs]
from razorpay.errors import SignatureVerificationError  # pyright: ignore[reportMissingTypeStubs]

from webhook_sync.config import Mode
from webhook_sync.queue import Failure
from webhook_sync.sources.base import (
    BusinessRow,
    EventDescription,
    Fetched,
    FetchResult,
    PaymentStatus,
    Rejection,
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
    ) -> None:
        self._mode = mode
        self._metadata_keys = tuple(metadata_keys)
        self._account = account
        self._secrets = tuple(webhook_secrets)
        self._api = api

    async def fetch(self, payment_key: str) -> FetchResult:
        """The payment's current state, with its refunds and card expanded."""
        try:
            response = await self._api.get(
                f"/v1/payments/{payment_key}",
                params=[("expand[]", "refunds"), ("expand[]", "card")],
            )
        except httpx.TransportError as error:  # a timeout, a refused or dropped connection
            return Failure(f"fetch failed: {type(error).__name__}", "retry")
        response.raise_for_status()
        state = cast(dict[str, Any], response.json())
        expanded = _object(state.get("refunds")).get("items")
        if isinstance(expanded, list) and len(cast(list[object], expanded)) >= EXPANDED_PAGE:
            refunds = await self._all_refunds(payment_key)
            if isinstance(refunds, Failure):
                return refunds
            state = {**state, "refunds": {"entity": "collection", "items": refunds}}
        return Fetched(state)

    async def _all_refunds(self, payment_key: str) -> list[dict[str, Any]] | Failure:
        """Every refund of the payment, paged 100 at a time until a page comes back short.

        A refunds collection's `count` is the items on that page, not a total, so a short page
        is the only sign of the end (the fetch recordings' README; #22)."""
        refunds: list[dict[str, Any]] = []
        while True:
            try:
                response = await self._api.get(
                    f"/v1/payments/{payment_key}/refunds",
                    params={"count": REFUNDS_PAGE, "skip": len(refunds)},
                )
            except httpx.TransportError as error:
                return Failure(f"refunds fetch failed: {type(error).__name__}", "retry")
            response.raise_for_status()
            page = cast(list[dict[str, Any]], response.json()["items"])
            refunds.extend(page)
            if len(page) < REFUNDS_PAGE:
                return refunds

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
        raise ValueError(f"unmapped Razorpay payment status {source_status!r}")
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


def payment_row(
    state: Mapping[str, Any], *, metadata_keys: Sequence[str] = ()
) -> BusinessRow | None:
    """A fetched Razorpay payment as its business row, or None while it is still `created`.

    A captured payment (or `refunded` with `captured: true`) is succeeded, partially_refunded or
    refunded by its processed refunds against the amount; `refunded` uncaptured is canceled (A7).
    Fee and fee tax (GST) are null until capture: pending. They are in the settlement currency
    (`base_currency`, else `currency`), and net is `(base_amount or amount) - fee` (Q130).
    """
    source_status: str = state["status"]
    if source_status == "created":
        return None  # nothing attempted yet: no row (G41)
    amount: int = state["amount"]
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
    )
