"""FakeRazorpay: Razorpay's API and its webhook deliveries, replaying the P0 recordings (Q163).

The one fake of Razorpay every in-process test uses, served through respx. It answers the
payments and refunds lists and the payment fetch as Razorpay does, on Razorpay's own clock, and
sends recorded deliveries to the receiver: once, many times, under a different event id, or with
a different body under the same id. What it knows is what the invariants hold the ledger to.
"""

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import format_datetime
from typing import Any

import httpx
import respx
from support import RAZORPAY_RECORDINGS, recorded_fetch

RAZORPAY_API = "https://api.razorpay.test"


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


@dataclass
class FakeRazorpay:
    """Razorpay's payments and refunds lists and its payment fetch, on Razorpay's own clock.

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

    def mount(self, router: respx.MockRouter) -> None:
        router.get(f"{RAZORPAY_API}/v1/payments").mock(side_effect=self._list_payments)
        router.get(f"{RAZORPAY_API}/v1/refunds").mock(side_effect=self._list_refunds)
        router.get(url__regex=rf"^{RAZORPAY_API}/v1/payments/(?P<payment_id>pay_\w+)(\?|$)").mock(
            side_effect=self._fetch
        )

    def calls_to(self, path: str) -> list[dict[str, str]]:
        return [query for called, query in self.list_calls if called == path]

    async def _list_payments(self, request: httpx.Request) -> httpx.Response:
        return await self._list(request, list(self.payments.values()))

    async def _list_refunds(self, request: httpx.Request) -> httpx.Response:
        return await self._list(request, self.refunds)

    async def _list(self, request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
        query = dict(request.url.params)
        self.list_calls.append((request.url.path, query))
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

    def _fetch(self, request: httpx.Request, payment_id: str) -> httpx.Response:
        return httpx.Response(200, json=self.fetches[payment_id])
