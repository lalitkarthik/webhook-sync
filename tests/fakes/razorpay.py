"""FakeRazorpay: Razorpay's API and its webhook deliveries, replaying the P0 recordings (Q163).

The one fake of Razorpay every test uses, served through respx in process and over HTTP to a
`wsync serve` subprocess (`fakes.over_http`). It answers the payments and refunds lists and the
payment fetch as Razorpay does, on Razorpay's own clock, taking `latency` seconds a call, and
sends recorded deliveries to the receiver: once, many times, under a different event id, or with
a different body under the same id. It replays the recorded refunds step by step, and a refund
that fails from Razorpay's documented sample (Q166). What it knows is what the invariants hold
the ledger to.
"""

import asyncio
import json
import random
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from email.utils import format_datetime
from typing import Any, Literal

import httpx
import respx
from support import (
    RAZORPAY_RECORDINGS,
    RECORDED_ACCOUNT,
    razorpay_sample,
    recorded_delivery,
    recorded_fetch,
    sign_razorpay,
)

RAZORPAY_API = "https://api.razorpay.test"
LIKE = "pay_Tj9rKBOXWcsMeE"
"""The recorded capture a made payment copies: netbanking, INR 500, its fee set."""
CAPTURED_OF_LIKE = "Tj9sMcl7hE9PaT"
"""The recorded `payment.captured` event of `LIKE`, whose bytes verify (the recordings' README)."""
ERROR_FIELDS = ("error_code", "error_description", "error_reason", "error_source", "error_step")


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


def recorded_refund(
    refund_id: str, status: Literal["pending", "processed"] = "processed"
) -> dict[str, Any]:
    """A recorded refund as Razorpay lists it, `processed`; or `pending`, as it stood when
    created: no bank reference yet."""
    [refund] = [item for item in recorded_listed_refunds() if item["id"] == refund_id]
    if status == "pending":
        return refund | {"status": "pending", "acquirer_data": {"arn": None}}
    return refund


def recorded_fetches() -> dict[str, dict[str, Any]]:
    """Each recorded payment fetch (refunds and card expanded), by payment id."""
    fetched: dict[str, dict[str, Any]] = {}
    for path in (RAZORPAY_RECORDINGS / "fetch").glob("payment-*-pay_*.json"):
        body = recorded_fetch(path.stem)
        fetched[body["id"]] = body
    return fetched


HANDLED_EVENTS = (
    "payment.authorized",
    "payment.captured",
    "payment.failed",
    "refund.created",
    "refund.processed",
    "refund.failed",
    "refund.speed_changed",
)
WEBHOOK_URL = "https://wsync-fixture.example/webhooks/razorpay"


def listed_webhook(url: str = WEBHOOK_URL, **changes: Any) -> dict[str, Any]:
    """A webhook as `GET /v1/webhooks` lists one for a merchant key (seen on the test account,
    2026-10-04): active, with a secret, every handled event ticked and one other not."""
    events = dict.fromkeys(HANDLED_EVENTS, True) | {"order.paid": True, "invoice.paid": False}
    webhook: dict[str, Any] = {
        "id": "Tj5fixtureWebhk",
        "entity": "webhook",
        "service": "api-test",
        "owner_id": RECORDED_ACCOUNT.removeprefix("acc_"),
        "owner_type": "merchant",
        "url": url,
        "active": True,
        "disabled_at": 0,
        "secret_exists": True,
        "events": events,
        "created_at": 1790000000,
        "updated_at": 1790000000,
    }
    return webhook | changes


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
class Payment:
    """A payment as Razorpay lists it and as it answers its fetch."""

    listed: dict[str, Any]
    fetched: dict[str, Any]


@dataclass(frozen=True)
class Sent:
    """A delivery sent to the receiver, and its answer."""

    delivery: Delivery
    status_code: int
    answer: Any
    elapsed: float = 0.0
    """Seconds from sending the delivery to its answer."""


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
    lists_down: bool = False
    """While set, every list call answers 503: Razorpay's lists down for a while."""
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
    shifting: bool = False
    """Items created in the same second come back in another order on every list call:
    Razorpay doesn't document their order (razorpay-list-facts.md)."""
    listed_late: timedelta = timedelta(0)
    """How long after a payment is made the payments list first shows it; its fetch and its
    order's payments show it at once (seen on the test account, 2026-10-05:
    razorpay-list-facts.md, Q7)."""
    arriving: list[Payment] = field(default_factory=list[Payment])
    """Payments made between two page calls: one is made each time a payments list call has
    answered a full page."""
    webhooks: list[dict[str, Any]] = field(default_factory=lambda: [listed_webhook()])
    """What `GET /v1/webhooks` lists (see `listed_webhook`)."""
    webhooks_status: int = 200
    """What the webhook list answers: Razorpay documents it only for partners, so a merchant
    key may get a 4xx."""
    key_refused: bool = False
    """Every list call answers 401, as Razorpay does to a key it doesn't know."""
    orders: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    """Every order made with `POST /v1/orders`, by id."""
    orders_busy: int = 0
    """The next this many calls for an order's payments answer 429, as Razorpay does to a
    caller that asks too often (seen on the test account, 2026-10-04)."""

    def mount(self, router: respx.MockRouter) -> None:
        router.get(f"{RAZORPAY_API}/v1/payments").mock(side_effect=self._list_payments)
        router.get(f"{RAZORPAY_API}/v1/refunds").mock(side_effect=self._list_refunds)
        router.get(f"{RAZORPAY_API}/v1/webhooks").mock(side_effect=self._list_webhooks)
        router.get(url__regex=rf"^{RAZORPAY_API}/v1/payments/(?P<payment_id>pay_\w+)(\?|$)").mock(
            side_effect=self._fetch
        )
        router.get(
            url__regex=rf"^{RAZORPAY_API}/v1/payments/(?P<payment_id>pay_\w+)/refunds(\?|$)"
        ).mock(side_effect=self._refunds)
        router.post(f"{RAZORPAY_API}/v1/orders").mock(side_effect=self._create_order)
        router.get(
            url__regex=rf"^{RAZORPAY_API}/v1/orders/(?P<order_id>order_\w+)/payments(\?|$)"
        ).mock(side_effect=self._order_payments)
        router.post(f"{RAZORPAY_API}/v1/payments/create/checkout").mock(side_effect=self._checkout)

    def pay(self, order_id: str) -> str:
        """The order is paid at Checkout, now: a captured netbanking payment of its amount (a
        copy of `LIKE`) on the order, known to the lists and the fetch, with no delivery. Its
        id."""
        order = self.orders[order_id]
        payment_id = f"pay_Checkout{len(self.orders_paid()):04d}"
        on_order = {"order_id": order_id, "amount": order["amount"]}
        made = self.made(payment_id, int(self.now.timestamp()))
        self._add(Payment(made.listed | on_order, made.fetched | on_order))
        self.orders[order_id] = order | {
            "status": "paid",
            "amount_paid": order["amount"],
            "amount_due": 0,
            "attempts": order["attempts"] + 1,
        }
        return payment_id

    def orders_paid(self) -> list[str]:
        """The payments made on an order, oldest first."""
        return [p for p, payment in self.payments.items() if payment.get("order_id")]

    def change(self, payment_id: str, state: dict[str, Any]) -> None:
        """The payment moves on at Razorpay: fetches answer `state` from now on."""
        self.earlier.setdefault(payment_id, []).append(self.fetches[payment_id])
        self.fetches[payment_id] = state

    def never(self, delivery: Delivery) -> None:
        """Razorpay makes the delivery's payment and the delivery never arrives: the payment is
        known from now on, and nothing is sent."""
        self._know(delivery.payment_id)

    def made(self, payment_id: str, created_at: int) -> Payment:
        """A payment created at `created_at`: a copy of the recorded capture `LIKE` under
        another id, as listed and as fetched."""
        moved = {"id": payment_id, "created_at": created_at}
        return Payment(recorded_listed_payments()[LIKE] | moved, recorded_fetches()[LIKE] | moved)

    def make(self, payment_id: str, created_at: int) -> None:
        """Razorpay makes the payment (see `made`), and sends no delivery."""
        self._add(self.made(payment_id, created_at))

    def authorise_late(self, payment_id: str) -> None:
        """A failed payment is authorised late and then captured (C13), with no delivery: its
        fetch goes through `authorized` to `captured`, and the list shows it captured. The fee
        is the recorded capture's of the same amount and method (`LIKE`)."""
        failed = self.fetches[payment_id]
        assert failed["status"] == "failed", failed["status"]
        fee = {name: recorded_fetches()[LIKE][name] for name in ("fee", "tax")}
        cleared = dict.fromkeys(ERROR_FIELDS)
        self.change(payment_id, failed | cleared | {"status": "authorized"})
        self.change(payment_id, failed | cleared | fee | {"status": "captured", "captured": True})
        self.payments[payment_id] = (
            self.payments[payment_id] | cleared | fee | {"status": "captured", "captured": True}
        )

    def before_refunds(self, payment_id: str) -> None:
        """A recorded payment as it stood before its refunds: captured, nothing refunded, and
        known from now on. `refund` then replays each of its refunds."""
        none = {"amount_refunded": 0, "refund_status": None}
        refunds: dict[str, Any] = {"entity": "collection", "count": 0, "items": []}
        self.fetches[payment_id] = self.fetches[payment_id] | none | {"refunds": refunds}
        self.payments[payment_id] = recorded_listed_payments()[payment_id] | none

    def refund(self, refund: dict[str, Any]) -> None:
        """Razorpay creates the refund, or moves it on to this state, with no delivery: it is
        on the refunds list and in its payment's fetch, newest first. The payment's
        `amount_refunded` and `refund_status` count every refund that has not failed: whether
        Razorpay's count a pending one is undocumented (G65), and the row must not follow them
        either way. Once they add up to the amount the payment is `refunded`, `full`, as
        Razorpay documents it (no recording shows one); below it, `captured` again."""
        payment_id = refund["payment_id"]
        state = self.fetches[payment_id]
        others = [other for other in state["refunds"]["items"] if other["id"] != refund["id"]]
        items = sorted([*others, refund], key=lambda item: item["created_at"], reverse=True)
        counted = sum(item["amount"] for item in items if item["status"] != "failed")
        full = counted >= state["amount"]
        totals = {
            "status": "refunded" if full else "captured",
            "amount_refunded": counted,
            "refund_status": "full" if full else "partial" if counted else None,
        }
        collection = {"entity": "collection", "count": len(items), "items": items}
        self.change(payment_id, state | totals | {"refunds": collection})
        self.payments[payment_id] = self.payments[payment_id] | totals
        self.refunds = [other for other in self.refunds if other["id"] != refund["id"]] + [refund]

    def documented_refund(
        self,
        payment_id: str,
        *,
        amount: int,
        sample: Literal["refund.failed", "refund.processed"] = "refund.failed",
    ) -> dict[str, Any]:
        """A refund no recording holds: one that failed, which test mode can't make (Q166), or
        a `processed` one. It is the refund of Razorpay's documented sample of that event,
        placed on the payment (its id and currency, `amount`, created now). Unrecorded: mark
        the test `unrecorded`."""
        documented: dict[str, Any] = razorpay_sample(sample)["payload"]["refund"]["entity"]
        return documented | {
            "payment_id": payment_id,
            "amount": amount,
            "currency": self.fetches[payment_id]["currency"],
            "created_at": int(self.now.timestamp()),
        }

    def documented_delivery(self, event_type: str, refund_id: str) -> Delivery:
        """Razorpay's documented sample of a refund event, created now for the recorded account,
        carrying the refund and its payment as they stand now, and signed with the fixture
        webhook secret. Unrecorded (Q166): the sample has no headers, so the event id is made
        up and the others are the recorded deliveries'."""
        [refund] = [item for item in self.refunds if item["id"] == refund_id]
        fetched = self.fetches[refund["payment_id"]]
        payment = {name: value for name, value in fetched.items() if name != "refunds"}
        event_id = f"unrecorded-{event_type}-{refund_id}"
        body = razorpay_sample(event_type) | {
            "account_id": RECORDED_ACCOUNT,
            "created_at": int(self.now.timestamp()),
            "payload": {"refund": {"entity": refund}, "payment": {"entity": payment}},
        }
        raw, signed = sign_razorpay(body, event_id=event_id)
        headers = {"user-agent": "Razorpay-Webhook/v1", "request-id": event_id, **signed}
        return Delivery(raw, headers)

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

    def captured_delivery(self, payment_id: str) -> Delivery:
        """The `payment.captured` delivery Razorpay sends for a payment made at Checkout
        (`pay`): the recorded one of `LIKE`, its payment that payment as fetched now."""
        recorded = json.loads(recorded_delivery(CAPTURED_OF_LIKE))
        recorded["payload"]["payment"]["entity"] = self.fetches[payment_id]
        return self.delivery(CAPTURED_OF_LIKE).with_body(recorded)

    async def deliver(
        self, client: httpx.AsyncClient, delivery: Delivery, *, times: int = 1
    ) -> list[Sent]:
        """Send `delivery` to the receiver `times` times at once, as Razorpay's retries and a
        replay storm do; the answers, also kept in `sent`. A delivery the server never answers
        is kept in `unanswered` instead. The payment it names exists at Razorpay from now on:
        it joins `payments`, from the recorded list or its fetch."""
        self._know(delivery.payment_id)

        async def send() -> Sent | None:
            try:
                answer = await client.post(
                    "/webhooks/razorpay", content=delivery.body, headers=delivery.headers
                )
            except httpx.TransportError:
                self.unanswered.append(delivery)
                return None
            sent = Sent(delivery, answer.status_code, answer.json(), answer.elapsed.total_seconds())
            self.sent.append(sent)
            return sent

        answers = await asyncio.gather(*(send() for _ in range(times)))
        return [sent for sent in answers if sent is not None]

    def _know(self, payment_id: str | None) -> None:
        """The payment exists at Razorpay: it joins `payments`, from the recorded list or its
        fetch."""
        if payment_id is not None and payment_id not in self.payments:
            listed = recorded_listed_payments()
            self.payments[payment_id] = listed.get(payment_id) or self.fetches[payment_id]

    def _add(self, payment: Payment) -> None:
        self.payments[payment.listed["id"]] = payment.listed
        self.fetches[payment.fetched["id"]] = payment.fetched

    def calls_to(self, path: str) -> list[dict[str, str]]:
        return [query for called, query in self.list_calls if called == path]

    async def _list_payments(self, request: httpx.Request) -> httpx.Response:
        shown = list(self.payments.values())
        if self.listed_late:
            old_enough = int((self.now - self.listed_late).timestamp())
            shown = [payment for payment in shown if payment["created_at"] <= old_enough]
        answer = await self._list(request, shown)
        full = answer.status_code == 200 and answer.json()["count"] == int(
            request.url.params.get("count", "10")
        )
        if full and self.arriving:
            self._add(self.arriving.pop(0))
        return answer

    async def _list_webhooks(self, request: httpx.Request) -> httpx.Response:
        date = {"date": format_datetime(self.now, usegmt=True)}
        if self.webhooks_status != 200:
            error = {"code": "BAD_REQUEST_ERROR", "description": "The requested URL was not found"}
            return httpx.Response(self.webhooks_status, json={"error": error}, headers=date)
        body = {"entity": "collection", "count": len(self.webhooks), "items": self.webhooks}
        return httpx.Response(200, json=body, headers=date)

    async def _list_refunds(self, request: httpx.Request) -> httpx.Response:
        return await self._list(request, self.refunds)

    async def _list(self, request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
        query = dict(request.url.params)
        self.list_calls.append((request.url.path, query))
        await asyncio.sleep(self.latency)
        if self.key_refused:
            return httpx.Response(
                401,
                json={
                    "error": {"code": "BAD_REQUEST_ERROR", "description": "Authentication failed"}
                },
                headers={"date": format_datetime(self.now, usegmt=True)},
            )
        start, end = int(query["from"]), int(query["to"])
        if self.lists_down:
            return httpx.Response(503, json={"error": {"description": "down"}})
        if start == self.fail_at:
            self.fail_at = None
            return httpx.Response(503, json={"error": {"description": "down"}})
        if start == self.hold_at:
            await self.release.wait()
        count, skip = int(query.get("count", "10")), int(query.get("skip", "0"))
        if self.shifting:
            items = random.Random(len(self.list_calls)).sample(items, len(items))
        newest_first = sorted(items, key=lambda item: item["created_at"], reverse=True)
        inside = [item for item in newest_first if start <= item["created_at"] <= end]
        page = inside[skip : skip + count]
        return httpx.Response(
            200,
            json={"entity": "collection", "count": len(page), "items": page},
            headers={"date": format_datetime(self.now, usegmt=True)},
        )

    async def _create_order(self, request: httpx.Request) -> httpx.Response:
        asked = json.loads(request.content)
        order_id = f"order_Demo{len(self.orders):010d}"
        self.orders[order_id] = {
            "id": order_id,
            "entity": "order",
            "amount": asked["amount"],
            "amount_paid": 0,
            "amount_due": asked["amount"],
            "currency": asked["currency"],
            "receipt": asked.get("receipt"),
            "status": "created",
            "attempts": 0,
            "notes": asked.get("notes", []),
            "created_at": int(self.now.timestamp()),
        }
        return httpx.Response(200, json=self.orders[order_id])

    async def _order_payments(self, request: httpx.Request, order_id: str) -> httpx.Response:
        if self.orders_busy:
            self.orders_busy -= 1
            error = {"code": "BAD_REQUEST_ERROR", "description": "Too many requests"}
            return httpx.Response(429, json={"error": error})
        if order_id not in self.orders:
            return httpx.Response(400, json=recorded_fetch("not-found-well-formed"))
        items = [
            self.fetches[payment_id]
            for payment_id in self.orders_paid()
            if self.payments[payment_id]["order_id"] == order_id
        ]
        return httpx.Response(
            200, json={"entity": "collection", "count": len(items), "items": items}
        )

    async def _checkout(self, request: httpx.Request) -> httpx.Response:
        """What Checkout's page asks of Razorpay once a person picks Success: the order paid.
        Only the fake payer calls it; a real browser goes through Razorpay's own pages."""
        order_id = dict(httpx.QueryParams(request.content.decode()))["order_id"]
        return httpx.Response(200, json={"razorpay_payment_id": self.pay(order_id)})

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
        refunds: dict[str, Any] = self.fetches.get(payment_id, {}).get("refunds") or {}
        items: list[dict[str, Any]] = refunds.get("items") or []
        page = items[skip : skip + count]
        return httpx.Response(200, json={"count": len(page), "entity": "collection", "items": page})
