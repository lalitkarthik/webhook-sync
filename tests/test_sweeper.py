"""The sweeper: payments that exist only at Razorpay come back through the sweep (#39).

Seam: the running app. Its lifespan starts the sweeper beside the ledger stage and the flusher;
Razorpay's lists and fetches are answered by respx from the recordings (`FakeRazorpay`, whose
clock is its own, as Razorpay's is), and Google by P3's fake Sheets. What comes out is what an
outsider sees: events with their origin, `payments` rows, checkpoints and the sheet. SQLite is
real; our clock is moved with time-machine and keeps ticking, so the API's token bucket refills.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from fakes.sheets import FakeSheets
from fastapi import FastAPI
from support import (
    RAZORPAY_RECORDINGS,
    RECORDED_ACCOUNT,
    TEST_SPREADSHEET_ID,
    ReadRows,
    recorded_api_answer,
    recorded_delivery,
    recorded_fetch,
    sign_razorpay,
)
from time_machine import TimeMachineFixture

from webhook_sync.config import Config, SweeperConfig
from webhook_sync.db.writer import Writer
from webhook_sync.queue import record_pause
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
    `hold_at` makes a list call wait while `release` is clear (a sweep caught mid-slice)."""

    now: datetime
    payments: dict[str, dict[str, Any]] = field(default_factory=dict[str, dict[str, Any]])
    refunds: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    fetches: dict[str, dict[str, Any]] = field(default_factory=recorded_fetches)
    list_calls: list[tuple[str, dict[str, str]]] = field(
        default_factory=list[tuple[str, dict[str, str]]]
    )
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
BOTH_LISTS_SWEPT = "SELECT count(*) AS n FROM checkpoints WHERE last_sweep_at IS NOT NULL"
CAUGHT_UP = (
    "SELECT count(*) AS n FROM checkpoints WHERE checkpoint_at = '2026-10-02T19:59:00.000000Z'"
)
"""Both lists' checkpoints at Razorpay's now (`NOW`) less a minute: every slice stored."""


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


def at(moment: datetime) -> int:
    return int(moment.timestamp())


async def checkpoint(read_rows: ReadRows, list_name: str) -> str | None:
    rows = await read_rows(f"SELECT checkpoint_at FROM checkpoints WHERE list = '{list_name}'")
    return rows[0]["checkpoint_at"] if rows else None


async def test_a_sweep_cancelled_mid_slice_re_reads_that_slice_and_stores_nothing_twice(
    swept_config: Config,
    data_dir: Path,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
) -> None:
    # A day back from 22:00 in 6 h slices: the last runs from 16:00, so its overlap re-reads
    # the two payments of 15:56 and 15:58 that the slice before it stored.
    now = datetime(2026, 10, 2, 22, 0, tzinfo=UTC)
    clock.move_to(now, tick=True)
    fake = outside.razorpay
    fake.now = now
    listed = recorded_listed_payments()
    for payment_id in OUTAGE:
        fake.payments[payment_id] = listed[payment_id]
    last_slice = at(datetime(2026, 10, 2, 16, 0, tzinfo=UTC))
    fake.hold_at = last_slice - 600  # the last slice's call waits: cancelled mid-slice

    async with running(swept_config, data_dir):
        for _ in range(500):
            if any(int(query["from"]) == fake.hold_at for query in fake.calls_to("/v1/payments")):
                break
            await asyncio.sleep(0.01)
    stopped_at = await checkpoint(read_rows, "payments")
    stored_before = await read_rows("SELECT payment_key FROM events ORDER BY payment_key")

    fake.hold_at = None
    fake.release.set()
    async with running(swept_config, data_dir):
        await wait_for(read_rows, ALL_ROWS, settled(OUTAGE))
        await wait_for(read_rows, f"SELECT count(*) AS n FROM ({MIRRORED})", [{"n": 3}])

    assert stopped_at == "2026-10-02T16:00:00.000000Z"
    assert stored_before == [
        {"payment_key": "pay_Tj64grZUMO3PEj"},
        {"payment_key": "pay_Tj66x2JuEXjIdB"},
    ]
    restarted = int(datetime.fromisoformat(stopped_at).timestamp())
    assert any(int(query["from"]) == restarted - 600 for query in fake.calls_to("/v1/payments"))
    assert await read_rows(
        "SELECT payment_key, count(*) AS n FROM events GROUP BY payment_key ORDER BY payment_key"
    ) == [{"payment_key": payment_id, "n": 1} for payment_id in OUTAGE]
    assert await checkpoint(read_rows, "payments") == "2026-10-02T21:59:00.000000Z"


async def test_a_quiet_accounts_checkpoints_still_advance_to_razorpays_now_less_a_minute(
    swept_config: Config, data_dir: Path, outside: Outside, read_rows: ReadRows
) -> None:
    outside.razorpay.now = NOW + timedelta(seconds=7)  # Razorpay's clock, not ours

    async with running(swept_config, data_dir):
        await wait_for(
            read_rows,
            "SELECT list, checkpoint_at, last_sweep_stored FROM checkpoints"
            " WHERE last_sweep_at IS NOT NULL ORDER BY list",
            [
                {
                    "list": name,
                    "checkpoint_at": "2026-10-02T19:59:07.000000Z",
                    "last_sweep_stored": 0,
                }
                for name in ("payments", "refunds")
            ],
        )

    assert await read_rows("SELECT * FROM events") == []
    # One call for Razorpay's now, then a day back from it in four slices of up to 6 h: the
    # first read from its start, each later one from 10 minutes before its start, and each up
    # to its end, the last at Razorpay's now less a minute, never a time still to come.
    calls = outside.razorpay.calls_to("/v1/payments")
    first = at(outside.razorpay.now - timedelta(days=1))
    assert [(int(q["from"]), int(q["to"])) for q in calls[1:]] == [
        (first, first + 6 * 3600),
        (first + 6 * 3600 - 600, first + 12 * 3600),
        (first + 12 * 3600 - 600, first + 18 * 3600),
        (first + 18 * 3600 - 600, at(outside.razorpay.now) - 60),
    ]


async def test_with_no_lookback_the_first_sweep_starts_from_now_and_stores_nothing_older(
    swept_config: Config,
    data_dir: Path,
    outside: Outside,
    read_rows: ReadRows,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sweeper_module, "SWEEP_EVERY", timedelta(milliseconds=300))
    config = replace(swept_config, sweeper=SweeperConfig(initial_lookback_days=0))
    fake = outside.razorpay
    fake.now = NOW - timedelta(seconds=20)  # Razorpay's clock is behind ours
    listed = recorded_listed_payments()
    for payment_id in OUTAGE:  # 21 minutes to 4 hours old
        fake.payments[payment_id] = listed[payment_id]
    # Five minutes before the start: inside a slice's 10-minute overlap, still older.
    fake.payments["pay_Tj66x2JuEXjIdB"] = {
        **listed["pay_Tj66x2JuEXjIdB"],
        "created_at": at(NOW - timedelta(minutes=5)),
    }

    async with running(config, data_dir):
        await wait_for(read_rows, "SELECT count(*) AS n FROM checkpoints", [{"n": 2}])
        first = await read_rows("SELECT list, checkpoint_at FROM checkpoints ORDER BY list")
        # Ten minutes on, a payment made after the start is swept; the older ones still aren't.
        fake.payments["pay_Tj64grZUMO3PEj"] = {
            **listed["pay_Tj64grZUMO3PEj"],
            "created_at": at(NOW + timedelta(minutes=5)),
        }
        fake.now = NOW + timedelta(minutes=10)
        await wait_for(read_rows, BOTH_LISTS_SWEPT, [{"n": 2}])
        await wait_for(
            read_rows,
            "SELECT payment_key FROM events",
            [{"payment_key": "pay_Tj64grZUMO3PEj"}],
        )

    started = "2026-10-02T19:59:40.000000Z"  # Razorpay's now at the first sweep
    assert first == [
        {"list": "payments", "checkpoint_at": started},
        {"list": "refunds", "checkpoint_at": started},
    ]
    # The first slice is read from the start itself, without the overlap.
    froms = {int(query["from"]) for query in fake.calls_to("/v1/payments")}
    assert min(froms) == at(NOW - timedelta(seconds=20))
    assert await read_rows("SELECT payment_key FROM events") == [
        {"payment_key": "pay_Tj64grZUMO3PEj"}
    ]
    assert await checkpoint(read_rows, "payments") == "2026-10-02T20:09:00.000000Z"
    # Caught up, a slice is requested up to Razorpay's now less a minute, not 6 h ahead.
    slices = [q for q in fake.calls_to("/v1/payments") if q["from"] != q["to"]]
    assert max(int(q["to"]) for q in slices) == at(NOW + timedelta(minutes=9))


async def test_while_razorpay_is_paused_the_sweeper_makes_no_list_calls(
    swept_config: Config,
    data_dir: Path,
    writer: Writer,
    outside: Outside,
    read_rows: ReadRows,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="webhook_sync.sweeper")
    async with writer.transaction() as tx:
        await record_pause(tx, "source", "razorpay", "Razorpay answered HTTP 401", now=NOW)
    outside.razorpay.payments.update(recorded_listed_payments())

    async with running(swept_config, data_dir):
        for _ in range(500):
            if any(r.message == "sweep skipped: Razorpay is paused" for r in caplog.records):
                break
            await asyncio.sleep(0.01)

    assert any(r.message == "sweep skipped: Razorpay is paused" for r in caplog.records)
    assert outside.razorpay.list_calls == []
    assert await read_rows("SELECT * FROM checkpoints") == []
    assert await read_rows("SELECT * FROM events") == []


async def test_each_listed_payment_and_refund_is_stored_as_a_synthetic_event_like_a_delivery(
    swept_config: Config, data_dir: Path, outside: Outside, read_rows: ReadRows
) -> None:
    outside.razorpay.payments.update(recorded_listed_payments())  # 7, two still `created`
    outside.razorpay.refunds.extend(recorded_listed_refunds())  # 2, of pay_Tj9gmEfed6kRqO

    async with running(swept_config, data_dir):
        await wait_for(read_rows, CAUGHT_UP, [{"n": 2}])
        await wait_for(
            read_rows,
            "SELECT payment_id, status FROM payments ORDER BY payment_id",
            [
                {"payment_id": "pay_Tj64grZUMO3PEj", "status": "succeeded"},
                {"payment_id": "pay_Tj66x2JuEXjIdB", "status": "succeeded"},
                {"payment_id": "pay_Tj9gmEfed6kRqO", "status": "partially_refunded"},
                {"payment_id": "pay_Tj9rKBOXWcsMeE", "status": "succeeded"},
                {"payment_id": "pay_Tj9sm13R8h2378", "status": "failed"},
            ],
        )

    events = await read_rows("SELECT * FROM events ORDER BY source_created_at")
    assert [(e["type"], e["payment_key"], e["event_id"].split(":")[1]) for e in events] == [
        ("payment.captured", "pay_Tj64grZUMO3PEj", "pay_Tj64grZUMO3PEj"),
        ("payment.captured", "pay_Tj66x2JuEXjIdB", "pay_Tj66x2JuEXjIdB"),
        ("payment.captured", "pay_Tj9gmEfed6kRqO", "pay_Tj9gmEfed6kRqO"),
        ("payment.captured", "pay_Tj9rKBOXWcsMeE", "pay_Tj9rKBOXWcsMeE"),
        ("payment.failed", "pay_Tj9sm13R8h2378", "pay_Tj9sm13R8h2378"),
        ("refund.processed", "pay_Tj9gmEfed6kRqO", "rfnd_Tj9tB2hm5WLvww"),
        ("refund.processed", "pay_Tj9gmEfed6kRqO", "rfnd_Tj9tMhKlOulF3Q"),
    ]
    first = events[0]
    prefix, entity_id, state_hash = first["event_id"].split(":")
    assert (prefix, entity_id, len(state_hash)) == ("sweep", "pay_Tj64grZUMO3PEj", 64)
    assert {
        k: first[k]
        for k in ("account", "mode", "origin", "outcome", "body_sha256", "source_created_at")
    } == {
        "account": RECORDED_ACCOUNT,
        "mode": "test",
        "origin": "sweep",
        "outcome": "queued",
        "body_sha256": None,
        "source_created_at": "2026-10-02T15:56:51.000000Z",  # the payment's created_at
    }
    assert len({e["fingerprint"] for e in events}) == len(events)
    assert all(e["job_id"] is not None for e in events)
    payload = json.loads(first["payload"])
    assert payload["event"] == "payment.captured"
    entity = payload["payload"]["payment"]["entity"]
    assert entity["id"] == "pay_Tj64grZUMO3PEj"
    assert not {"email", "notes", "acquirer_data", "description"} & set(entity)
    assert await read_rows("SELECT * FROM deliveries") == []


async def test_a_changed_state_re_read_in_the_overlap_is_a_new_event_and_the_same_one_is_not(
    swept_config: Config,
    data_dir: Path,
    outside: Outside,
    read_rows: ReadRows,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sweeper_module, "SWEEP_EVERY", timedelta(milliseconds=300))
    fake = outside.razorpay
    created = at(NOW - timedelta(minutes=3))  # inside every later slice's 10-minute overlap
    authorized = recorded_api_answer("20261003T010947-pay_Tj9rKBOXWcsMeE-authorized")
    captured = recorded_api_answer("20261003T010957-pay_Tj9rKBOXWcsMeE-captured")
    fake.payments["pay_Tj9rKBOXWcsMeE"] = {**authorized, "created_at": created}

    async with running(swept_config, data_dir):
        await wait_for(read_rows, "SELECT type FROM events", [{"type": "payment.authorized"}])
        fake.payments["pay_Tj9rKBOXWcsMeE"] = {**captured, "created_at": created}
        fake.now = NOW + timedelta(minutes=5)
        await wait_for(
            read_rows,
            "SELECT type FROM events ORDER BY received_at",
            [{"type": "payment.authorized"}, {"type": "payment.captured"}],
        )
        fake.now = NOW + timedelta(minutes=8)  # re-read again, unchanged
        await wait_for(
            read_rows,
            "SELECT checkpoint_at FROM checkpoints WHERE list = 'payments'",
            [{"checkpoint_at": "2026-10-02T20:07:00.000000Z"}],
        )

    assert await read_rows("SELECT count(*) AS n FROM events") == [{"n": 2}]


# The hourly re-list (#40): later changes to payments the slices have already passed.

AUTHORIZED = recorded_api_answer("20261003T010947-pay_Tj9rKBOXWcsMeE-authorized")
CAPTURED = recorded_api_answer("20261003T010957-pay_Tj9rKBOXWcsMeE-captured")
AN_HOUR_ON = NOW + timedelta(hours=1, minutes=1)


async def an_hour_later(clock: TimeMachineFixture, fake: FakeRazorpay) -> None:
    """Both clocks an hour on: the next sweep's slices are past every item, its re-list due."""
    clock.move_to(AN_HOUR_ON, tick=True)
    fake.now = AN_HOUR_ON


@pytest.fixture
def frequent_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sweeper_module, "SWEEP_EVERY", timedelta(milliseconds=300))


@pytest.mark.usefixtures("frequent_sweeps")
async def test_a_payment_captured_after_it_was_swept_is_re_listed_and_its_row_succeeds_with_the_fee(
    swept_config: Config,
    data_dir: Path,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
) -> None:
    fake = outside.razorpay
    fake.payments["pay_Tj9rKBOXWcsMeE"] = AUTHORIZED  # created 21 minutes before NOW
    fake.fetches["pay_Tj9rKBOXWcsMeE"] = AUTHORIZED
    row = "SELECT status, fee_minor FROM payments WHERE payment_id = 'pay_Tj9rKBOXWcsMeE'"

    async with running(swept_config, data_dir):
        await wait_for(read_rows, row, [{"status": "authorized", "fee_minor": None}])
        fake.payments["pay_Tj9rKBOXWcsMeE"] = CAPTURED
        fake.fetches["pay_Tj9rKBOXWcsMeE"] = CAPTURED
        await an_hour_later(clock, fake)
        await wait_for(read_rows, row, [{"status": "succeeded", "fee_minor": 1180}])

    assert await read_rows("SELECT type, origin FROM events ORDER BY received_at") == [
        {"type": "payment.authorized", "origin": "sweep"},
        {"type": "payment.captured", "origin": "sweep"},
    ]
    # The re-list read the last 7 days of payments, up to Razorpay's now.
    relists = [
        q
        for q in fake.calls_to("/v1/payments")
        if int(q["from"]) == at(AN_HOUR_ON - timedelta(days=7))
    ]
    assert relists
    assert {int(q["to"]) for q in relists} == {at(AN_HOUR_ON)}


ALL_LEDGER_JOBS_DONE = "SELECT count(*) AS n FROM jobs WHERE stage = 'ledger' AND status != 'done'"


def with_refunds(payment: dict[str, Any], status: str) -> dict[str, Any]:
    """The payment's fetch with each of its refunds in `status`."""
    items = [{**refund, "status": status} for refund in payment["refunds"]["items"]]
    return {**payment, "refunds": {**payment["refunds"], "items": items}}


@pytest.mark.usefixtures("frequent_sweeps")
async def test_a_refund_going_from_pending_to_processed_updates_the_refunded_amount_on_the_re_list(
    swept_config: Config,
    data_dir: Path,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
) -> None:
    fake = outside.razorpay
    fetched = fake.fetches["pay_Tj9gmEfed6kRqO"]
    fake.payments["pay_Tj9gmEfed6kRqO"] = recorded_listed_payments()["pay_Tj9gmEfed6kRqO"]
    fake.refunds.extend(
        recorded_api_answer(name)
        for name in (
            "20261003T011044-rfnd_Tj9tB2hm5WLvww-pending",
            "20261003T011054-rfnd_Tj9tMhKlOulF3Q-pending",
        )
    )
    fake.fetches["pay_Tj9gmEfed6kRqO"] = with_refunds(fetched, "pending")
    row = "SELECT status, refunded_minor FROM payments WHERE payment_id = 'pay_Tj9gmEfed6kRqO'"

    async with running(swept_config, data_dir):
        await wait_for(read_rows, CAUGHT_UP, [{"n": 2}])
        await wait_for(read_rows, row, [{"status": "succeeded", "refunded_minor": 0}])
        await wait_for(read_rows, ALL_LEDGER_JOBS_DONE, [{"n": 0}])
        fake.refunds[:] = [
            recorded_api_answer(name)
            for name in (
                "20261003T011109-rfnd_Tj9tB2hm5WLvww-processed",
                "20261003T011111-rfnd_Tj9tMhKlOulF3Q-processed",
            )
        ]
        fake.fetches["pay_Tj9gmEfed6kRqO"] = fetched
        await an_hour_later(clock, fake)
        await wait_for(read_rows, row, [{"status": "partially_refunded", "refunded_minor": 20000}])

    assert await read_rows(
        "SELECT type, count(*) AS n FROM events WHERE type LIKE 'refund.%' GROUP BY type"
        " ORDER BY type"
    ) == [{"type": "refund.created", "n": 2}, {"type": "refund.processed", "n": 2}]
    relists = [
        q
        for q in fake.calls_to("/v1/refunds")
        if int(q["from"]) == at(AN_HOUR_ON - timedelta(days=21))
    ]
    assert relists


@pytest.mark.usefixtures("frequent_sweeps")
async def test_a_re_list_in_which_nothing_changed_stores_no_event_and_enqueues_no_job(
    swept_config: Config,
    data_dir: Path,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
) -> None:
    fake = outside.razorpay
    fake.payments.update(recorded_listed_payments())
    fake.refunds.extend(recorded_listed_refunds())
    relisted = "SELECT count(*) AS n FROM checkpoints WHERE last_relist_at > '2026-10-02T21:00'"

    async with running(swept_config, data_dir):
        await wait_for(read_rows, CAUGHT_UP, [{"n": 2}])
        await wait_for(read_rows, ALL_LEDGER_JOBS_DONE, [{"n": 0}])
        events = await read_rows("SELECT event_id FROM events ORDER BY event_id")
        jobs = await read_rows("SELECT id FROM jobs ORDER BY id")
        await an_hour_later(clock, fake)
        await wait_for(read_rows, relisted, [{"n": 2}])

    assert len(events) == 7
    assert await read_rows("SELECT event_id FROM events ORDER BY event_id") == events
    assert await read_rows("SELECT id FROM jobs ORDER BY id") == jobs


# Backfill (#40): older payments on demand, never touching a checkpoint.

IST = ZoneInfo("Asia/Kolkata")


@asynccontextmanager
async def started(config: Config, data_dir: Path) -> AsyncGenerator[FastAPI]:
    """The app, started (its lifespan run), for what its state holds."""
    app = create_app(config, data_dir)
    async with app.router.lifespan_context(app):
        yield app


async def test_a_backfill_of_past_local_dates_stores_backfill_events_and_moves_no_checkpoint(
    swept_config: Config, data_dir: Path, outside: Outside, read_rows: ReadRows
) -> None:
    fake = outside.razorpay
    listed = recorded_listed_payments()
    created = {
        "pay_Tj64grZUMO3PEj": datetime(2026, 8, 31, 23, 59, 59, tzinfo=IST),  # the day before
        "pay_Tj66x2JuEXjIdB": datetime(2026, 9, 1, 0, 0, tzinfo=IST),  # the first second
        "pay_Tj9rKBOXWcsMeE": datetime(2026, 9, 10, 23, 59, 59, tzinfo=IST),  # the last second
        "pay_Tj9sm13R8h2378": datetime(2026, 9, 11, 0, 0, tzinfo=IST),  # the day after
    }
    for payment_id, moment in created.items():
        fake.payments[payment_id] = {**listed[payment_id], "created_at": at(moment)}
    swept = "SELECT list, checkpoint_at, last_sweep_at, last_relist_at FROM checkpoints"

    async with started(swept_config, data_dir) as app:
        await wait_for(read_rows, CAUGHT_UP, [{"n": 2}])
        before = await read_rows(swept)
        stored = await app.state.sweeper.backfill(date(2026, 9, 1), date(2026, 9, 10))
        # Via is `sweep` for any swept origin (data-model.md): backfill is a sweep on demand.
        await wait_for(read_rows, ALL_ROWS, settled(("pay_Tj66x2JuEXjIdB", "pay_Tj9rKBOXWcsMeE")))

    assert stored == 2
    events = await read_rows("SELECT event_id, origin FROM events ORDER BY payment_key")
    assert [(e["event_id"].split(":")[:2], e["origin"]) for e in events] == [
        (["backfill", "pay_Tj66x2JuEXjIdB"], "backfill"),
        (["backfill", "pay_Tj9rKBOXWcsMeE"], "backfill"),
    ]
    assert await read_rows(swept) == before
    backfills = [q for q in fake.calls_to("/v1/payments") if q["from"] != q["to"]]
    assert (backfills[-1]["from"], backfills[-1]["to"]) == (
        str(at(datetime(2026, 9, 1, tzinfo=IST))),
        str(at(datetime(2026, 9, 11, tzinfo=IST)) - 1),
    )


# The heartbeat (#40): one POST after each healthy pass, so an outside service notices silence.

HEARTBEAT = "https://hc-ping.test/a-check"


def requests(router: respx.MockRouter) -> list[httpx.Request]:
    """Every request the router answered, in order."""
    calls = cast(list[respx.models.Call], list(router.calls))
    return [call.request for call in calls]


def heartbeats(router: respx.MockRouter) -> list[httpx.Request]:
    return [request for request in requests(router) if request.url.host == "hc-ping.test"]


@pytest.fixture
def heartbeat_config(swept_config: Config) -> Config:
    return replace(swept_config, sweeper=replace(swept_config.sweeper, heartbeat_url=HEARTBEAT))


async def test_with_a_heartbeat_url_each_healthy_pass_is_followed_by_one_post(
    heartbeat_config: Config,
    data_dir: Path,
    outside: Outside,
    read_rows: ReadRows,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sweeper_module, "SWEEP_EVERY", timedelta(milliseconds=200))
    outside.router.post(HEARTBEAT).mock(return_value=httpx.Response(200, text="OK"))

    async with running(heartbeat_config, data_dir):
        await wait_for(read_rows, CAUGHT_UP, [{"n": 2}])
        for _ in range(500):
            if len(heartbeats(outside.router)) >= 2:
                break
            await asyncio.sleep(0.01)
        passes = len(outside.razorpay.calls_to("/v1/refunds"))  # the walk's probe, at least

    pings = heartbeats(outside.router)
    assert len(pings) >= 2
    assert all(ping.method == "POST" for ping in pings)
    assert len(pings) <= passes


async def test_no_heartbeat_follows_a_failed_pass(
    heartbeat_config: Config,
    data_dir: Path,
    outside: Outside,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="webhook_sync.sweeper")
    outside.router.post(HEARTBEAT).mock(return_value=httpx.Response(200, text="OK"))
    outside.router.get(f"{RAZORPAY_API}/v1/refunds").mock(
        return_value=httpx.Response(503, json={"error": {"description": "down"}})
    )

    async with running(heartbeat_config, data_dir):
        for _ in range(500):
            if any(r.message == "sweep stopped: the list failed" for r in caplog.records):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)

    assert any(r.message == "sweep stopped: the list failed" for r in caplog.records)
    assert heartbeats(outside.router) == []


async def test_no_heartbeat_while_razorpay_is_paused(
    heartbeat_config: Config,
    data_dir: Path,
    writer: Writer,
    outside: Outside,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="webhook_sync.sweeper")
    outside.router.post(HEARTBEAT).mock(return_value=httpx.Response(200, text="OK"))
    async with writer.transaction() as tx:
        await record_pause(tx, "source", "razorpay", "Razorpay answered HTTP 401", now=NOW)

    async with running(heartbeat_config, data_dir):
        for _ in range(500):
            if any(r.message == "sweep skipped: Razorpay is paused" for r in caplog.records):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)

    assert any(r.message == "sweep skipped: Razorpay is paused" for r in caplog.records)
    assert heartbeats(outside.router) == []


async def test_with_no_heartbeat_url_a_healthy_pass_posts_nowhere(
    swept_config: Config, data_dir: Path, outside: Outside, read_rows: ReadRows
) -> None:
    async with running(swept_config, data_dir):
        await wait_for(read_rows, CAUGHT_UP, [{"n": 2}])
        await asyncio.sleep(0.1)

    hosts = {request.url.host for request in requests(outside.router)}
    assert hosts <= {"api.razorpay.test", "oauth2.googleapis.com", "sheets.googleapis.com"}
    assert "api.razorpay.test" in hosts


async def test_a_heartbeat_that_fails_is_logged_and_not_retried(
    heartbeat_config: Config,
    data_dir: Path,
    outside: Outside,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="webhook_sync.sweeper")
    outside.router.post(HEARTBEAT).mock(return_value=httpx.Response(500))

    async with running(heartbeat_config, data_dir):
        for _ in range(500):
            if any(r.message.startswith("heartbeat not sent") for r in caplog.records):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)

    assert [r.message for r in caplog.records if r.message.startswith("heartbeat")] == [
        "heartbeat not sent: HTTPStatusError"
    ]
    assert len(heartbeats(outside.router)) == 1
