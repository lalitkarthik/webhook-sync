"""FakeRazorpay: Razorpay's API and its webhook deliveries, replaying the P0 recordings (Q163).

The one fake of Razorpay every test uses, served through respx in process and over HTTP to a
`wsync serve` subprocess (`fakes.over_http`). It answers the payments and refunds lists and the
payment fetch as Razorpay does, on Razorpay's own clock, taking `latency` seconds a call, and
sends recorded deliveries to the receiver: once, many times, under a different event id, or with
a different body under the same id. What it knows is what the invariants hold the ledger to.
"""

import asyncio
import json
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime
from email.utils import format_datetime
from typing import Any

import httpx
import respx
from support import RAZORPAY_RECORDINGS, recorded_delivery, recorded_fetch, sign_razorpay

RAZORPAY_API = "https://api.razorpay.test"


def recorded_latency() -> float:
    """The median time, in seconds, one of Razorpay's API calls took when recorded (Q167)."""
    path = RAZORPAY_RECORDINGS / "api" / "list" / "headers-timing.json"
    timings: list[int] = json.loads(path.read_text())["payments_sequential_ms"]
    return statistics.median(timings) / 1000


def recorded_listed_payments() -> dict[str, dict[str, Any]]:
    """The recorded payments list's items by id, as Razorpay lists them (no refunds expanded)."""
    path = RAZORPAY_RECORDINGS / "api" / "list" / "payments-list-count100.json"
    items = json.loads(path.read_text())["body"]["items"]
    return {item["id"]: item for item in items}


def recorded_listed_refunds() -> list[dict[str, Any]]:
    path = RAZORPAY_RECORDINGS / "api" / "list" / "refunds-list-count100.json"
    return json.loads(path.read_text())["body"]["items"]


def recorded_fetches() -> dict[str, dict[str, Any]]:
    """Each recorded payment fetch (refunds and card expanded), by payment id."""
    fetched: dict[str, dict[str, Any]] = {}
    for path in (RAZORPAY_RECORDINGS / "fetch").glob("payment-*-pay_*.json"):
        body = recorded_fetch(path.stem)
        fetched[body["id"]] = body
    return fetched


@dataclass(frozen=True)
class Delivery:
    """One webhook delivery as Razorpay sends it: the raw body, and the recorded headers with
    the body signed by the fixture webhook secret (the recording's own signature used the real
    one)."""

    body: bytes
    headers: dict[str, str]

    @property
    def event_id(self) -> str:
        return self.headers["x-razorpay-event-id"]

    @property
    def signature(self) -> str:
        return self.headers["x-razorpay-signature"]

    @property
    def payment_id(self) -> str | None:
        """The payment the event names, or None."""
        payload: dict[str, Any] = json.loads(self.body).get("payload", {})
        payment: dict[str, Any] = payload.get("payment", {}).get("entity", {})
        return payment.get("id")

    def under(self, event_id: str) -> Delivery:
        """The same bytes and signature under another `x-razorpay-event-id`: the header is not
        signed (F12), so only the signed-body hash can tell it is the same event."""
        return replace(
            self, headers=self.headers | {"x-razorpay-event-id": event_id, "request-id": event_id}
        )

    def with_body(self, body: dict[str, Any] | bytes) -> Delivery:
        """Another body under the same event id, signed with the fixture secret."""
        raw, signed = sign_razorpay(body, event_id=self.event_id)
        return replace(self, body=raw, headers=self.headers | signed)


@dataclass(frozen=True)
class Sent:
    """A delivery sent to the receiver, and its answer."""

    delivery: Delivery
    status_code: int
    answer: Any


@dataclass
class FakeRazorpay:
    """Razorpay's payments and refunds lists, its payment fetch and a payment's refunds pages,
    on Razorpay's own clock.

    A list answers as Razorpay does: newest first, `from` and `to` both inclusive, `count` items
    after `skip`, with `now` in the `Date` header. A fetch answers the payment's recorded state.
    `hold_at` makes a list call wait while `release` is clear (a sweep caught mid-slice)."""

    now: datetime
    payments: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    refunds: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    fetches: dict[str, dict[str, Any]] = field(default_factory=recorded_fetches)
    list_calls: list[tuple[str, dict[str, str]]] = field(
        default_factory=list[tuple[str, dict[str, str]]]
    )
    fail_at: int | None = None
    """A list call whose `from` is this answers 503, once."""
    hold_at: int | None = None
    """A list call whose `from` is this waits until `release` is set."""
    release: asyncio.Event = field(default_factory=asyncio.Event)
    sent: list[Sent] = field(default_factory=list[Sent])
    """Every delivery sent to the receiver so far, in the order answered."""
    earlier: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict[str, list[dict[str, Any]]]
    )
    """The states each changed payment had before its current one in `fetches`, oldest first."""
    unanswered: list[Delivery] = field(default_factory=list[Delivery])
    """Every delivery sent whose connection closed with no answer: the server died."""
    latency: float = 0.0
    """Seconds each list call and fetch takes before it answers; `recorded_latency()` is
    Razorpay's own."""

    def mount(self, router: respx.MockRouter) -> None:
        router.get(f"{RAZORPAY_API}/v1/payments").mock(side_effect=self._list_payments)
        router.get(f"{RAZORPAY_API}/v1/refunds").mock(side_effect=self._list_refunds)
        router.get(url__regex=rf"^{RAZORPAY_API}/v1/payments/(?P<payment_id>pay_\w+)(\?|$)").mock(
            side_effect=self._fetch
        )
        router.get(
            url__regex=rf"^{RAZORPAY_API}/v1/payments/(?P<payment_id>pay_\w+)/refunds(\?|$)"
        ).mock(side_effect=self._refunds)

    def change(self, payment_id: str, state: dict[str, Any]) -> None:
        """The payment moves on at Razorpay: fetches answer `state` from now on."""
        self.earlier.setdefault(payment_id, []).append(self.fetches[payment_id])
        self.fetches[payment_id] = state

    def states(self, payment_id: str) -> list[dict[str, Any]]:
        """Every state the payment has had at Razorpay, oldest first; the last is current."""
        current = [self.fetches[payment_id]] if payment_id in self.fetches else []
        return [*self.earlier.get(payment_id, []), *current]

    def delivery(self, event_id: str, attempt: int = 1) -> Delivery:
        """The delivery P0 recorded (`tests/recordings/razorpay/<id>-<attempt>.json`), its body
        re-signed with the fixture webhook secret. The config's account must be
        RECORDED_ACCOUNT."""
        path = RAZORPAY_RECORDINGS / f"{event_id}-{attempt}.json"
        recorded: dict[str, str] = json.loads(path.read_text())["headers"]
        body, signed = sign_razorpay(recorded_delivery(event_id, attempt), event_id=event_id)
        headers = {
            "user-agent": recorded["User-Agent"],
            "request-id": recorded["Request-Id"],
            **signed,
        }
        return Delivery(body, headers)

    async def deliver(
        self, client: httpx.AsyncClient, delivery: Delivery, *, times: int = 1
    ) -> list[Sent]:
        """Send `delivery` to the receiver `times` times at once, as Razorpay's retries and a
        replay storm do; the answers, also kept in `sent`. A delivery the server never answers
        is kept in `unanswered` instead. The payment it names exists at Razorpay from now on:
        it joins `payments`, from the recorded list or its fetch."""
        payment_id = delivery.payment_id
        if payment_id is not None and payment_id not in self.payments:
            listed = recorded_listed_payments()
            self.payments[payment_id] = listed.get(payment_id) or self.fetches[payment_id]

        async def send() -> Sent | None:
            try:
                answer = await client.post(
                    "/webhooks/razorpay", content=delivery.body, headers=delivery.headers
                )
            except httpx.TransportError:
                self.unanswered.append(delivery)
                return None
            sent = Sent(delivery, answer.status_code, answer.json())
            self.sent.append(sent)
            return sent

        answers = await asyncio.gather(*(send() for _ in range(times)))
        return [sent for sent in answers if sent is not None]

    def calls_to(self, path: str) -> list[dict[str, str]]:
        return [query for called, query in self.list_calls if called == path]

    async def _list_payments(self, request: httpx.Request) -> httpx.Response:
        return await self._list(request, list(self.payments.values()))

    async def _list_refunds(self, request: httpx.Request) -> httpx.Response:
        return await self._list(request, self.refunds)

    async def _list(self, request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
        query = dict(request.url.params)
        self.list_calls.append((request.url.path, query))
        await asyncio.sleep(self.latency)
        start, end = int(query["from"]), int(query["to"])
        if start == self.fail_at:
            self.fail_at = None
            return httpx.Response(503, json={"error": {"description": "down"}})
        if start == self.hold_at:
            await self.release.wait()
        count, skip = int(query.get("count", "10")), int(query.get("skip", "0"))
        newest_first = sorted(items, key=lambda item: item["created_at"], reverse=True)
        inside = [item for item in newest_first if start <= item["created_at"] <= end]
        page = inside[skip : skip + count]
        return httpx.Response(
            200,
            json={"entity": "collection", "count": len(page), "items": page},
            headers={"date": format_datetime(self.now, usegmt=True)},
        )

    async def _fetch(self, request: httpx.Request, payment_id: str) -> httpx.Response:
        await asyncio.sleep(self.latency)
        if payment_id not in self.fetches:
            return httpx.Response(400, json=recorded_fetch("not-found-well-formed"))
        return httpx.Response(200, json=self.fetches[payment_id])

    async def _refunds(self, request: httpx.Request, payment_id: str) -> httpx.Response:
        """A page of the payment's refunds, in its fetch's order: an empty page past the end,
        and for a payment Razorpay doesn't know, as recorded."""
        await asyncio.sleep(self.latency)
        query = dict(request.url.params)
        count, skip = int(query.get("count", "10")), int(query.get("skip", "0"))
        refunds = self.fetches.get(payment_id, {}).get("refunds") or {}
        page = (refunds.get("items") or [])[skip : skip + count]
        return httpx.Response(200, json={"count": len(page), "entity": "collection", "items": page})
