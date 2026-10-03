"""Verify, Razorpay against ledger: every night the ledger is proved against Razorpay (#42).

Seam: the running app. Its lifespan runs the full verify once per local date at `[verify]
nightly_at` in `service.timezone`, caught up at start when the server was down then, beside the
sweeper, the ledger stage and the flusher. Razorpay's lists and fetches are answered by respx from
the recordings (the sweeper tests' `FakeRazorpay`), Google by P3's fake Sheets and the alerts
webhook by respx. Every payment was created ten days before the night, so neither the sweep nor
an hourly re-list brings it in: only verify can. What comes out is what an outsider sees:
`verify_runs`, `verify_findings`, `payments`, the sheet and the alerts. SQLite is real; our
clock is moved with time-machine and keeps ticking.
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import respx
from fakes.sheets import FakeSheets
from respx import Route
from support import RECORDED_ACCOUNT, TEST_SPREADSHEET_ID, ReadRows
from test_sweeper import (
    RAZORPAY_API,
    FakeRazorpay,
    recorded_listed_payments,
    recorded_listed_refunds,
)
from time_machine import TimeMachineFixture

from webhook_sync.clock import utc_now
from webhook_sync.config import AlertsConfig, Config, SweeperConfig, VerifyConfig
from webhook_sync.db.writer import Writer
from webhook_sync.queue import enqueue
from webhook_sync.queue import runner as runner_module
from webhook_sync.receiver.app import create_app
from webhook_sync.sinks import sheets as sheets_module
from webhook_sync.sinks.sheets import PAYMENTS
from webhook_sync.sweeper import sweep as sweeper_module

HOOK = "https://hooks.example.test/services/T000/B000/fixture"
EARLY = datetime(2026, 10, 11, 19, 30, tzinfo=UTC)  # 01:00 IST on 12 Oct: the night not yet due
LATE = datetime(2026, 10, 11, 21, 30, 5, tzinfo=UTC)  # 03:00:05 IST: 02:00 missed
CAPTURED = "pay_Tj9rKBOXWcsMeE"  # captured on 3 Oct, fee 1180
PARTLY_REFUNDED = "pay_Tj9gmEfed6kRqO"  # captured on 3 Oct, two processed refunds of 100


@pytest.fixture(autouse=True)
def clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(EARLY, tick=True)
    return time_machine


@pytest.fixture(autouse=True)
def quick_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stage runners' idle waits short, so a flush follows a job at once."""
    monkeypatch.setattr(runner_module, "IDLE_WAIT", timedelta(milliseconds=10))
    monkeypatch.setattr(sheets_module, "FLUSH_EVERY", timedelta(milliseconds=30))


@pytest.fixture
def verified_config(config: Config) -> Config:
    """The test config as the recordings need it; the sweep starts empty, so it stays quiet."""
    return replace(
        config,
        razorpay=replace(config.razorpay, account=RECORDED_ACCOUNT, api_url=RAZORPAY_API),
        sweeper=SweeperConfig(initial_lookback_days=0),
        alerts=AlertsConfig(url=HOOK),
    )


@dataclass
class Outside:
    razorpay: FakeRazorpay
    sheets: FakeSheets
    hook: Route

    def alerts(self) -> list[str]:
        calls: list[Any] = list(self.hook.calls)  # pyright: ignore[reportUnknownArgumentType]
        return [json.loads(call.request.content)["text"] for call in calls]


@pytest.fixture
def outside() -> Iterator[Outside]:
    """Razorpay, Google and the alerts webhook, all faked through one respx router."""
    razorpay = FakeRazorpay(now=EARLY)
    sheets = FakeSheets(TEST_SPREADSHEET_ID)
    with respx.mock(assert_all_called=False) as router:
        razorpay.mount(router)
        sheets.mount(router)
        hook = router.post(HOOK).respond(200, text="ok")
        yield Outside(razorpay, sheets, hook)


@asynccontextmanager
async def running(config: Config, data_dir: Path) -> AsyncGenerator[None]:
    app = create_app(config, data_dir)
    async with app.router.lifespan_context(app):
        yield


DEADLINE = 30.0
"""Real seconds a background loop gets to show what a test waits for."""


async def until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + DEADLINE
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


async def wait_for(read_rows: ReadRows, sql: str, expected: list[dict[str, Any]]) -> None:
    """Wait (in real time) until `sql` reads `expected`: the app's loops work in the
    background."""
    deadline = time.monotonic() + DEADLINE
    while time.monotonic() <= deadline:
        if await read_rows(sql) == expected:
            return
        await asyncio.sleep(0.01)
    assert await read_rows(sql) == expected


FULL_RUNS = "SELECT count(*) AS n FROM verify_runs WHERE kind = 'full' AND finished_at IS NOT NULL"
RUNS = (
    "SELECT kind, local_date, payments_checked, findings_count, repaired_count, status_line"
    " FROM verify_runs ORDER BY id"
)
FINDINGS = (
    "SELECT kind, payment_id, detail, repair_job_id IS NOT NULL AS repaired FROM verify_findings"
    " ORDER BY run_id, kind, payment_id"
)
QUIET = "SELECT count(*) AS n FROM jobs WHERE status IN ('waiting', 'running')"


def listed(*payment_ids: str) -> dict[str, dict[str, Any]]:
    """The recorded payments list's items for these payments, by id."""
    items = recorded_listed_payments()
    return {payment_id: items[payment_id] for payment_id in payment_ids}


async def ledgered(
    config: Config, data_dir: Path, writer: Writer, read_rows: ReadRows, *payment_ids: str
) -> None:
    """The payments fetched into the ledger and written to the sheet before the night, the app
    stopped again."""
    async with writer.transaction() as tx:
        for payment_id in payment_ids:
            await enqueue(
                tx,
                stage="ledger",
                sink="ledger",
                source="razorpay",
                account=RECORDED_ACCOUNT,
                payment_key=payment_id,
                event_id=None,
                retry_profile="production",
                now=utc_now(),
            )
    async with running(config, data_dir):
        await wait_for(
            read_rows,
            "SELECT count(*) AS n FROM jobs WHERE stage = 'mirror' AND status = 'done'",
            [{"n": len(payment_ids)}],
        )
        await wait_for(read_rows, QUIET, [{"n": 0}])


@asynccontextmanager
async def night(
    config: Config,
    data_dir: Path,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
    outside: Outside,
    at: datetime = LATE,
    runs: int = 1,
) -> AsyncGenerator[None]:
    """The app started at `at`, held until `runs` full runs have finished and its jobs are
    done."""
    clock.move_to(at, tick=True)
    outside.razorpay.now = at
    async with running(config, data_dir):
        await wait_for(read_rows, FULL_RUNS, [{"n": runs}])
        await wait_for(read_rows, QUIET, [{"n": 0}])
        yield


def sheet_rows(sheets: FakeSheets) -> dict[str, dict[str, Any]]:
    """The Payments tab's rows by payment id, each as header → value."""
    header, *rows = sheets.tab(PAYMENTS).values()
    return {row[0]: dict(zip(header, row, strict=False)) for row in rows}


async def test_a_payment_only_at_razorpay_is_missing_and_repaired_into_the_ledger_and_sheet(
    verified_config: Config,
    data_dir: Path,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
) -> None:
    outside.razorpay.payments.update(listed(CAPTURED))

    async with night(verified_config, data_dir, read_rows, clock, outside):
        await wait_for(
            read_rows,
            "SELECT payment_key FROM jobs WHERE stage = 'mirror' AND status = 'done'",
            [{"payment_key": CAPTURED}],
        )

    assert await read_rows(RUNS) == [
        {
            "kind": "full",
            "local_date": "2026-10-12",
            "payments_checked": 1,
            "findings_count": 1,
            "repaired_count": 1,
            "status_line": "Verified 12 Oct 03:00 IST: last 30 days, 1 payment, 1 difference",
        }
    ]
    assert await read_rows(FINDINGS) == [
        {"kind": "missing", "payment_id": CAPTURED, "detail": None, "repaired": 1}
    ]
    assert await read_rows("SELECT payment_id, status, fee_minor FROM payments") == [
        {"payment_id": CAPTURED, "status": "succeeded", "fee_minor": 1180}
    ]
    assert list(sheet_rows(outside.sheets)) == [CAPTURED]
    assert outside.alerts() == []


async def test_a_ledger_row_whose_fee_differs_from_razorpays_is_stale_and_repaired(
    verified_config: Config,
    data_dir: Path,
    writer: Writer,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
) -> None:
    fake = outside.razorpay
    fake.payments.update(listed(CAPTURED))
    await ledgered(verified_config, data_dir, writer, read_rows, CAPTURED)
    # Razorpay's fee changed after the ledger fetched the payment, and no event announced it.
    fake.payments[CAPTURED] = fake.payments[CAPTURED] | {"fee": 1416, "tax": 216}
    fake.fetches[CAPTURED] = fake.fetches[CAPTURED] | {"fee": 1416, "tax": 216}

    async with night(verified_config, data_dir, read_rows, clock, outside):
        await wait_for(
            read_rows,
            "SELECT count(*) AS n FROM jobs WHERE stage = 'mirror' AND status = 'done'",
            [{"n": 2}],
        )

    assert await read_rows(FINDINGS) == [
        {
            "kind": "stale",
            "payment_id": CAPTURED,
            "detail": "fee_minor, fee_tax_minor, net_minor",
            "repaired": 1,
        }
    ]
    assert await read_rows("SELECT fee_minor, fee_tax_minor, net_minor FROM payments") == [
        {"fee_minor": 1416, "fee_tax_minor": 216, "net_minor": 48584}
    ]
    assert sheet_rows(outside.sheets)[CAPTURED]["Fee"] == 14.16
    assert outside.alerts() == []


async def test_a_ledger_payment_razorpay_doesnt_list_is_orphaned_and_not_changed(
    verified_config: Config,
    data_dir: Path,
    writer: Writer,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
) -> None:
    fake = outside.razorpay
    fake.payments.update(listed(CAPTURED))
    await ledgered(verified_config, data_dir, writer, read_rows, CAPTURED)
    before = await read_rows("SELECT * FROM payments")
    del fake.payments[CAPTURED]  # deleted test data, or another account's payment

    async with night(verified_config, data_dir, read_rows, clock, outside):
        await until(lambda: bool(outside.alerts()), "the verify_differences alert")

    assert await read_rows(FINDINGS) == [
        {"kind": "orphaned", "payment_id": CAPTURED, "detail": None, "repaired": 0}
    ]
    assert await read_rows("SELECT * FROM payments") == before
    assert await read_rows("SELECT count(*) AS n FROM jobs WHERE stage = 'ledger'") == [{"n": 1}]
    assert outside.alerts() == [
        "webhook-sync verify_differences: 1 difference(s) verify left unrepaired (orphaned 1)"
    ]


async def test_a_partly_refunded_payment_is_not_flagged(
    verified_config: Config,
    data_dir: Path,
    writer: Writer,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
) -> None:
    fake = outside.razorpay
    fake.payments.update(listed(PARTLY_REFUNDED))
    # Listed, its refunds come back empty: they are read from the refunds list.
    fake.refunds.extend(recorded_listed_refunds())
    await ledgered(verified_config, data_dir, writer, read_rows, PARTLY_REFUNDED)

    async with night(verified_config, data_dir, read_rows, clock, outside):
        pass

    assert await read_rows("SELECT status, refunded_minor FROM payments") == [
        {"status": "partially_refunded", "refunded_minor": 20000}
    ]
    assert await read_rows(FINDINGS) == []
    assert await read_rows(RUNS) == [
        {
            "kind": "full",
            "local_date": "2026-10-12",
            "payments_checked": 1,
            "findings_count": 0,
            "repaired_count": 0,
            "status_line": "Verified 12 Oct 03:00 IST: last 30 days, 1 payment, 0 differences",
        }
    ]


def logged(caplog: pytest.LogCaptureFixture, message: str) -> int:
    return sum(r.getMessage() == message for r in caplog.records)


async def test_the_full_run_happens_once_per_local_date_caught_up_at_start(
    verified_config: Config,
    data_dir: Path,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="webhook_sync.verify")

    # Started at 03:00, the server missed 02:00: today's run happens at start.
    async with night(verified_config, data_dir, read_rows, clock, outside):
        pass
    # Restarted at 05:00 the same day: nothing to run until tomorrow's 02:00.
    clock.move_to(LATE + timedelta(hours=2), tick=True)
    async with running(verified_config, data_dir):
        await until(lambda: logged(caplog, "nightly verify waiting") >= 1, "the next night")

    [run] = await read_rows("SELECT kind, local_date, started_at FROM verify_runs")
    assert (run["kind"], run["local_date"]) == ("full", "2026-10-12")
    assert run["started_at"].startswith("2026-10-11T21:30:0")  # at start, not at 05:00
    [waiting] = [r for r in caplog.records if r.getMessage() == "nightly verify waiting"]
    assert (waiting.__dict__["local_date"], waiting.__dict__["due_at"]) == (
        "2026-10-13",
        "2026-10-12T20:30:00.000000Z",
    )


async def test_a_server_running_at_nightly_at_runs_the_full_verify_then(
    verified_config: Config,
    data_dir: Path,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
) -> None:
    two = datetime(2026, 10, 11, 20, 30, tzinfo=UTC)  # 02:00 IST on 12 Oct

    async with night(
        verified_config, data_dir, read_rows, clock, outside, at=two - timedelta(seconds=1)
    ):
        pass

    [run] = await read_rows("SELECT local_date, started_at FROM verify_runs")
    assert run["local_date"] == "2026-10-12"
    assert run["started_at"] >= "2026-10-11T20:30:00"


async def test_the_same_payment_stale_on_two_nights_raises_one_stale_twice_alert(
    verified_config: Config,
    data_dir: Path,
    writer: Writer,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
) -> None:
    config = replace(verified_config, verify=VerifyConfig(auto_repair=False))
    fake = outside.razorpay
    fake.payments.update(listed(CAPTURED, PARTLY_REFUNDED))
    fake.refunds.extend(recorded_listed_refunds())
    await ledgered(config, data_dir, writer, read_rows, CAPTURED, PARTLY_REFUNDED)
    fake.payments[CAPTURED] = fake.payments[CAPTURED] | {"fee": 1416, "tax": 216}

    async with night(config, data_dir, read_rows, clock, outside):
        await until(lambda: len(outside.alerts()) == 1, "the first night's alert")
    async with night(
        config, data_dir, read_rows, clock, outside, at=LATE + timedelta(days=1), runs=2
    ):
        await until(lambda: len(outside.alerts()) == 3, "the second night's alerts")

    assert (
        await read_rows(FINDINGS)
        == [
            {
                "kind": "stale",
                "payment_id": CAPTURED,
                "detail": "fee_minor, fee_tax_minor, net_minor",
                "repaired": 0,
            }
        ]
        * 2
    )
    unrepaired = "webhook-sync verify_differences: 1 difference(s) verify left unrepaired (stale 1)"
    assert outside.alerts() == [
        unrepaired,
        unrepaired,
        "webhook-sync stale_twice: 1 payment(s) stale on two full verify runs in a row:"
        f" {CAPTURED}",
    ]
    assert await read_rows("SELECT local_date FROM verify_runs ORDER BY id") == [
        {"local_date": "2026-10-12"},
        {"local_date": "2026-10-13"},
    ]


async def test_a_payment_made_while_the_server_was_down_is_left_to_the_sweep(
    verified_config: Config,
    data_dir: Path,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sweeper_module, "START_DELAY", timedelta(0))
    async with running(verified_config, data_dir):  # up at 01:00: the sweep's checkpoints
        await wait_for(read_rows, "SELECT count(*) AS n FROM checkpoints", [{"n": 2}])
    # Down from 01:00 to 03:00; a payment made at 01:30 reached no webhook.
    made = int((EARLY + timedelta(minutes=30)).timestamp())
    outside.razorpay.payments[CAPTURED] = listed(CAPTURED)[CAPTURED] | {"created_at": made}
    # Back at 03:00, the full run goes first: the sweep waits (a year at most, so in practice
    # past the test), so the payment is still the sweep's to bring in, with Via = sweep.
    monkeypatch.setattr(sweeper_module, "START_DELAY", timedelta(days=365))

    async with night(verified_config, data_dir, read_rows, clock, outside):
        pass

    assert await read_rows(FINDINGS) == []
    assert await read_rows("SELECT payments_checked, status_line FROM verify_runs") == [
        {
            "payments_checked": 0,
            "status_line": "Verified 12 Oct 03:00 IST: last 30 days, 0 payments, 0 differences",
        }
    ]
    assert await read_rows("SELECT count(*) AS n FROM jobs WHERE stage = 'ledger'") == [{"n": 0}]
