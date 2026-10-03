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
from test_sweeper import RAZORPAY_API, FakeRazorpay, recorded_listed_payments
from time_machine import TimeMachineFixture

from webhook_sync.clock import utc_now
from webhook_sync.config import AlertsConfig, Config, SweeperConfig
from webhook_sync.db.writer import Writer
from webhook_sync.queue import enqueue
from webhook_sync.queue import runner as runner_module
from webhook_sync.receiver.app import create_app
from webhook_sync.sinks import sheets as sheets_module
from webhook_sync.sinks.sheets import PAYMENTS

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
