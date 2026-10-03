"""The sweeper: payments that exist only at Razorpay come back through the sweep (#39).

Seam: the running app. Its lifespan starts the sweeper beside the ledger stage and the flusher;
Razorpay's lists and fetches are answered by respx from the recordings (`FakeRazorpay`, whose
clock is its own, as Razorpay's is), and Google by P3's fake Sheets. What comes out is what an
outsider sees: events with their origin, `payments` rows, checkpoints and the sheet. SQLite is
real; our clock is moved with time-machine and keeps ticking, so the API's token bucket refills.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fakes.sheets import FakeSheets
from support import (
    RAZORPAY_RECORDINGS,
    RECORDED_ACCOUNT,
    TEST_SPREADSHEET_ID,
    ReadRows,
    recorded_delivery,
    recorded_fetch,
    sign_razorpay,
)
from time_machine import TimeMachineFixture

from webhook_sync.config import Config, SweeperConfig
from webhook_sync.queue import runner as runner_module
from webhook_sync.receiver.app import create_app
from webhook_sync.sinks import sheets as sheets_module
from webhook_sync.sinks.sheets import PAYMENTS
from webhook_sync.sweeper import sweep as sweeper_module

RAZORPAY_API = "https://api.razorpay.test"
NOW = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)  # after every recorded payment was created
OUTAGE = ("pay_Tj64grZUMO3PEj", "pay_Tj66x2JuEXjIdB", "pay_Tj9rKBOXWcsMeE")
NEVER_ATTEMPTED = "pay_Tj63Ohj3DwqGru"  # `created`: no event, no row


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
    `hold` makes list calls wait while it is clear (a sweep caught mid-slice)."""

    now: datetime
    payments: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    refunds: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    fetches: dict[str, dict[str, Any]] = field(default_factory=recorded_fetches)
    list_calls: list[tuple[str, dict[str, str]]] = field(
        default_factory=list[tuple[str, dict[str, str]]]
    )
    hold_from: int | None = None
    """List calls whose `from` is at least this wait until `release` is set."""
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
        if self.hold_from is not None and start >= self.hold_from:
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


@pytest.fixture(autouse=True)
def clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(NOW, tick=True)
    return time_machine


@pytest.fixture(autouse=True)
def quick_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """No start delay, and the stage runners' idle waits short, so a test takes a second."""
    monkeypatch.setattr(sweeper_module, "START_DELAY", timedelta(0))
    monkeypatch.setattr(runner_module, "IDLE_WAIT", timedelta(milliseconds=10))
    monkeypatch.setattr(sheets_module, "FLUSH_EVERY", timedelta(milliseconds=30))


@pytest.fixture
def swept_config(config: Config) -> Config:
    """The test config as the recordings need it, with a day's first-start lookback."""
    return replace(
        config,
        razorpay=replace(config.razorpay, account=RECORDED_ACCOUNT, api_url=RAZORPAY_API),
        sweeper=SweeperConfig(initial_lookback_days=1),
    )


@dataclass
class Outside:
    razorpay: FakeRazorpay
    sheets: FakeSheets
    router: respx.MockRouter


@pytest.fixture
def outside() -> Iterator[Outside]:
    """Razorpay and Google, both faked through one respx router."""
    razorpay = FakeRazorpay(now=NOW)
    sheets = FakeSheets(TEST_SPREADSHEET_ID)
    with respx.mock(assert_all_called=False) as router:
        razorpay.mount(router)
        sheets.mount(router)
        yield Outside(razorpay, sheets, router)


@asynccontextmanager
async def running(config: Config, data_dir: Path) -> AsyncGenerator[httpx.AsyncClient]:
    """The app, started (its lifespan run), and a client for its public routes."""
    app = create_app(config, data_dir)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://wsync.test") as client:
            yield client


async def wait_for(read_rows: ReadRows, sql: str, expected: list[dict[str, Any]]) -> None:
    """Wait (in real time) until `sql` reads `expected`: the app's loops work in the
    background."""
    for _ in range(500):
        if await read_rows(sql) == expected:
            return
        await asyncio.sleep(0.01)
    assert await read_rows(sql) == expected


def settled(ids: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{"payment_id": payment_id, "via": "sweep"} for payment_id in sorted(ids)]


def sheet_rows(sheets: FakeSheets) -> dict[str, dict[str, Any]]:
    """The Payments tab's rows by payment id, each as header → value."""
    header, *rows = sheets.tab(PAYMENTS).values()
    return {row[0]: dict(zip(header, row, strict=False)) for row in rows}


ALL_ROWS = "SELECT payment_id, via FROM payments ORDER BY payment_id"
MIRRORED = "SELECT payment_key FROM jobs WHERE stage = 'mirror' AND status = 'done'"


async def test_payments_made_while_the_server_was_down_reach_the_ledger_and_the_sheet(
    swept_config: Config, data_dir: Path, outside: Outside, read_rows: ReadRows
) -> None:
    listed = recorded_listed_payments()
    for payment_id in (*OUTAGE, NEVER_ATTEMPTED):
        outside.razorpay.payments[payment_id] = listed[payment_id]

    async with running(swept_config, data_dir):
        await wait_for(read_rows, ALL_ROWS, settled(OUTAGE))
        await wait_for(
            read_rows,
            f"{MIRRORED} ORDER BY payment_key",
            [{"payment_key": payment_id} for payment_id in sorted(OUTAGE)],
        )

    rows = sheet_rows(outside.sheets)
    assert sorted(rows) == sorted(OUTAGE)
    assert {row["Via"] for row in rows.values()} == {"sweep"}
    assert await read_rows("SELECT * FROM deliveries") == []
    events = await read_rows("SELECT origin, payment_key FROM events ORDER BY payment_key")
    assert events == [{"origin": "sweep", "payment_key": payment_id} for payment_id in OUTAGE]


async def test_a_late_delivery_of_a_swept_payment_is_a_new_event_that_changes_nothing(
    swept_config: Config, data_dir: Path, outside: Outside, read_rows: ReadRows
) -> None:
    listed = recorded_listed_payments()
    for payment_id in OUTAGE:
        outside.razorpay.payments[payment_id] = listed[payment_id]
    # Razorpay's own retry of the capture of pay_Tj9rKBOXWcsMeE, after the server came back.
    body, headers = sign_razorpay(recorded_delivery("Tj9sMcl7hE9PaT"), event_id="Tj9sMcl7hE9PaT")

    async with running(swept_config, data_dir) as client:
        await wait_for(read_rows, f"SELECT count(*) AS n FROM ({MIRRORED})", [{"n": 3}])
        delivered = await client.post("/webhooks/razorpay", content=body, headers=headers)
        await wait_for(
            read_rows,
            "SELECT status FROM jobs"
            " WHERE stage = 'ledger' AND caused_by_event_id = 'Tj9sMcl7hE9PaT'",
            [{"status": "done"}],
        )

    assert delivered.status_code == 202
    assert await read_rows(
        "SELECT origin FROM events WHERE payment_key = 'pay_Tj9rKBOXWcsMeE' ORDER BY origin"
    ) == [{"origin": "delivery"}, {"origin": "sweep"}]
    assert await read_rows(
        "SELECT via, row_version FROM payments WHERE payment_id = 'pay_Tj9rKBOXWcsMeE'"
    ) == [{"via": "sweep", "row_version": 1}]
    assert await read_rows("SELECT count(*) AS n FROM jobs WHERE stage = 'mirror'") == [{"n": 3}]
    assert sorted(sheet_rows(outside.sheets)) == sorted(OUTAGE)
