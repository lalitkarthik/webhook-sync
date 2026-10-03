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
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
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

from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.config import AlertsConfig, Config, SweeperConfig, VerifyConfig
from webhook_sync.db.schema import checkpoints
from webhook_sync.db.writer import Writer
from webhook_sync.queue import enqueue, record_pause
from webhook_sync.queue import runner as runner_module
from webhook_sync.receiver.app import create_app
from webhook_sync.sinks import sheets as sheets_module
from webhook_sync.sinks.sheets import PAYMENTS
from webhook_sync.sweeper import sweep as sweeper_module
from webhook_sync.verify import full as full_module

HOOK = "https://hooks.example.test/services/T000/B000/fixture"
EARLY = datetime(2026, 10, 11, 19, 30, tzinfo=UTC)  # 01:00 IST on 12 Oct: the night not yet due
TWO = datetime(2026, 10, 11, 20, 30, tzinfo=UTC)  # 02:00 IST on 12 Oct: `nightly_at`
LATE = datetime(2026, 10, 11, 21, 30, 5, tzinfo=UTC)  # 03:00:05 IST: 02:00 missed
CAPTURED = "pay_Tj9rKBOXWcsMeE"  # captured on 3 Oct, fee 1180
PARTLY_REFUNDED = "pay_Tj9gmEfed6kRqO"  # captured on 3 Oct, two processed refunds of 100


@pytest.fixture(autouse=True)
def clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(EARLY, tick=True)
    return time_machine


@pytest.fixture(autouse=True)
def quick_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stage runners' idle waits short, so a flush follows a job at once; the sweep at once
    at start, and the catch-up looking for it often."""
    monkeypatch.setattr(runner_module, "IDLE_WAIT", timedelta(milliseconds=10))
    monkeypatch.setattr(sheets_module, "FLUSH_EVERY", timedelta(milliseconds=30))
    monkeypatch.setattr(sweeper_module, "START_DELAY", timedelta(0))
    monkeypatch.setattr(full_module, "POLL_EVERY", timedelta(milliseconds=10))


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
    router: respx.MockRouter
    logs: pytest.LogCaptureFixture
    """Verify's log records, INFO and up."""

    def waiting_for(self) -> list[str]:
        """The local dates the nightly loop said it was waiting for, in order."""
        return [
            r.__dict__["local_date"]
            for r in self.logs.records
            if r.getMessage() == "nightly verify waiting"
        ]

    def alerts(self) -> list[str]:
        calls: list[Any] = list(self.hook.calls)  # pyright: ignore[reportUnknownArgumentType]
        return [json.loads(call.request.content)["text"] for call in calls]


@pytest.fixture
def outside(caplog: pytest.LogCaptureFixture) -> Iterator[Outside]:
    """Razorpay, Google and the alerts webhook, all faked through one respx router."""
    caplog.set_level(logging.INFO, logger="webhook_sync.verify")
    razorpay = FakeRazorpay(now=EARLY)
    sheets = FakeSheets(TEST_SPREADSHEET_ID)
    with respx.mock(assert_all_called=False) as router:
        razorpay.mount(router)
        sheets.mount(router)
        hook = router.post(HOOK).respond(200, text="ok")
        yield Outside(razorpay, sheets, hook, router, caplog)


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
    """The app started at `at`, held until the night is over."""
    async with back_at(config, data_dir, clock, outside, at):
        await night_over(config, read_rows, outside, at, runs)
        yield


@asynccontextmanager
async def back_at(
    config: Config, data_dir: Path, clock: TimeMachineFixture, outside: Outside, at: datetime
) -> AsyncGenerator[None]:
    """The app started at `at`, on both clocks."""
    clock.move_to(at, tick=True)
    outside.razorpay.now = at
    async with running(config, data_dir):
        yield


async def night_over(
    config: Config, read_rows: ReadRows, outside: Outside, at: datetime, runs: int = 1
) -> None:
    """Wait until `runs` full runs have finished, the loop has moved on to the night after
    `at`'s (after the run's alerts) and the jobs are done."""
    today = at.astimezone(config.service.timezone).date().isoformat()
    await wait_for(read_rows, FULL_RUNS, [{"n": runs}])
    await until(lambda: any(day > today for day in outside.waiting_for()), "the next night's wait")
    await wait_for(read_rows, QUIET, [{"n": 0}])


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


async def test_the_full_run_happens_once_per_local_date_caught_up_at_start(
    verified_config: Config,
    data_dir: Path,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
) -> None:
    # Started at 03:00, the server missed 02:00: today's run happens at start.
    async with night(verified_config, data_dir, read_rows, clock, outside):
        pass
    # Restarted at 05:00 the same day: nothing to run until tomorrow's 02:00.
    outside.logs.clear()
    clock.move_to(LATE + timedelta(hours=2), tick=True)
    async with running(verified_config, data_dir):
        await until(lambda: bool(outside.waiting_for()), "the restart's wait")

    [run] = await read_rows("SELECT kind, local_date, started_at FROM verify_runs")
    assert (run["kind"], run["local_date"]) == ("full", "2026-10-12")
    assert run["started_at"] < "2026-10-11T22:00"  # at the first start, not at 05:00
    [waiting] = [r for r in outside.logs.records if r.getMessage() == "nightly verify waiting"]
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
    async with night(
        verified_config, data_dir, read_rows, clock, outside, at=TWO - timedelta(seconds=1)
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


@pytest.mark.parametrize(
    "back", [LATE, TWO - timedelta(seconds=1)], ids=["after nightly_at", "just before nightly_at"]
)
async def test_a_payment_made_while_the_server_was_down_comes_in_through_the_sweep(
    back: datetime,
    verified_config: Config,
    data_dir: Path,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
) -> None:
    async with running(verified_config, data_dir):  # up at 01:00: the sweep's checkpoints
        await wait_for(read_rows, "SELECT count(*) AS n FROM checkpoints", [{"n": 2}])
    # Down from 01:00; a payment made at 01:30 reached no webhook.
    made = int((EARLY + timedelta(minutes=30)).timestamp())
    fake = outside.razorpay
    fake.payments[CAPTURED] = listed(CAPTURED)[CAPTURED] | {"created_at": made}
    fake.fetches[CAPTURED] = fake.fetches[CAPTURED] | {"created_at": made}
    fake.hold_at = int(EARLY.timestamp())  # the sweep's first slice, from the 01:00 checkpoint

    async with back_at(verified_config, data_dir, clock, outside, back):
        await until(
            lambda: any(int(q["from"]) == fake.hold_at for q in fake.calls_to("/v1/payments")),
            "the sweep's held slice",
        )
        # Past 02:00, the sweep still held: the night waits for it.
        await asyncio.sleep(max((TWO - utc_now()).total_seconds(), 0) + 0.3)
        assert await read_rows(FULL_RUNS) == [{"n": 0}]
        fake.release.set()
        await night_over(verified_config, read_rows, outside, back)
        await wait_for(
            read_rows,
            "SELECT payment_key FROM jobs WHERE stage = 'mirror' AND status = 'done'",
            [{"payment_key": CAPTURED}],
        )

    assert await read_rows(FINDINGS) == []
    assert await read_rows("SELECT payment_id, via FROM payments") == [
        {"payment_id": CAPTURED, "via": "sweep"}
    ]
    assert sheet_rows(outside.sheets)[CAPTURED]["Via"] == "sweep"


async def test_a_payment_a_stalled_sweeper_never_brought_in_is_missing(
    verified_config: Config,
    data_dir: Path,
    writer: Writer,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The sweeper last moved its checkpoints at 01:00 and has been stuck since.
    async with writer.transaction() as tx:
        for name in ("payments", "refunds"):
            await tx.execute(
                checkpoints.insert().values(
                    source="razorpay",
                    account=RECORDED_ACCOUNT,
                    list=name,
                    checkpoint_at=to_db_text(EARLY),
                    last_sweep_at=to_db_text(EARLY),
                    last_sweep_stored=0,
                )
            )
    monkeypatch.setattr(sweeper_module, "START_DELAY", timedelta(days=365))
    monkeypatch.setattr(full_module, "FIRST_SWEEP_WAIT", timedelta(milliseconds=200))
    made = int((EARLY + timedelta(minutes=30)).timestamp())
    outside.razorpay.payments[CAPTURED] = listed(CAPTURED)[CAPTURED] | {"created_at": made}

    async with night(verified_config, data_dir, read_rows, clock, outside):
        pass

    assert await read_rows(FINDINGS) == [
        {"kind": "missing", "payment_id": CAPTURED, "detail": None, "repaired": 1}
    ]
    assert await read_rows("SELECT payment_id FROM payments") == [{"payment_id": CAPTURED}]


def verify_lists_fail(outside: Outside, times: int | None) -> list[httpx.Request]:
    """The night's payments lists answer 503, `times` times (None: always); the sweep's lists
    are untouched. The failed requests, as they come."""
    probe = httpx.Request("GET", f"{RAZORPAY_API}/v1/payments?from=1&to=2")
    [route] = [r for r in outside.router.routes if r.pattern.match(probe)]
    answer: Callable[[httpx.Request], Awaitable[httpx.Response]] = route.side_effect  # pyright: ignore[reportAssignmentType]
    verify_from = int((LATE - timedelta(days=29)).timestamp())
    failed: list[httpx.Request] = []

    async def verify_list_fails(request: httpx.Request) -> httpx.Response:
        if int(request.url.params["from"]) < verify_from and (times is None or len(failed) < times):
            failed.append(request)
            return httpx.Response(503, json={"error": {"description": "unavailable"}})
        return await answer(request)

    route.side_effect = verify_list_fails
    return failed


async def test_a_night_still_failing_when_its_date_is_over_is_given_up_with_a_warning(
    verified_config: Config,
    data_dir: Path,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(full_module, "RETRY_EVERY", timedelta(milliseconds=50))
    failed = verify_lists_fail(outside, times=None)

    async with back_at(verified_config, data_dir, clock, outside, LATE):
        await until(lambda: len(failed) >= 2, "the night tried again")
        clock.move_to(datetime(2026, 10, 12, 18, 30, 1, tzinfo=UTC), tick=True)  # 00:00:01
        await until(lambda: "2026-10-13" in outside.waiting_for(), "the next night's wait")

    given_up = [
        r for r in outside.logs.records if r.getMessage().startswith("nightly verify given")
    ]
    assert [(r.levelname, r.__dict__["local_date"], r.__dict__["error"]) for r in given_up] == [
        ("WARNING", "2026-10-12", "Razorpay answered HTTP 503")
    ]
    assert await read_rows(FULL_RUNS) == [{"n": 0}]


async def test_a_night_whose_list_fails_is_tried_again_until_it_is_stored(
    verified_config: Config,
    data_dir: Path,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(full_module, "RETRY_EVERY", timedelta(milliseconds=50))
    outside.razorpay.payments.update(listed(CAPTURED))
    failed = verify_lists_fail(outside, times=1)

    async with night(verified_config, data_dir, read_rows, clock, outside):
        pass

    assert len(failed) == 1
    retried = [
        r.__dict__["local_date"]
        for r in outside.logs.records
        if r.getMessage() == "nightly verify failed; trying again"
    ]
    assert retried == ["2026-10-12"]
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


async def test_while_razorpay_is_paused_the_night_checks_only_the_sheet(
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
    async with writer.transaction() as tx:
        await record_pause(tx, "source", "razorpay", "Razorpay answered HTTP 401", now=utc_now())
    calls_before = len(fake.list_calls)

    clock.move_to(LATE, tick=True)
    async with running(verified_config, data_dir):
        await wait_for(
            read_rows,
            "SELECT count(*) AS n FROM verify_runs WHERE finished_at IS NOT NULL",
            [{"n": 1}],
        )

    assert await read_rows(RUNS) == [
        {
            "kind": "quick",
            "local_date": "2026-10-12",
            "payments_checked": 1,
            "findings_count": 0,
            "repaired_count": 0,
            "status_line": "Sheet checked 12 Oct 03:00 IST: 1 payment, 0 differences",
        }
    ]
    assert len(fake.list_calls) == calls_before
