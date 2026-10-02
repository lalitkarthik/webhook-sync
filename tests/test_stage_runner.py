"""The stage runner and the reaper, with a fake stage (seam 2: the queue).

Real SQLite in a fresh data directory, time moved with time-machine and never slept: the runner
is driven one pass at a time, and its heartbeat's sleep is injected. Assertions are on the
`jobs` and `attempts` rows.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from support import ReadRows
from time_machine import TimeMachineFixture

from webhook_sync.clock import utc_now
from webhook_sync.db.writer import Transaction, Writer
from webhook_sync.queue import (
    ClaimedJob,
    Done,
    Failure,
    Pauses,
    StageFailed,
    StageRunner,
    Work,
    claim,
    enqueue,
    reap,
    run_reaper,
)

NINE = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
INSTANCE = "wsync-test@2026-10-02T08:59:00.000000Z"


@pytest.fixture(autouse=True)
def frozen_clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(NINE, tick=False)
    return time_machine


async def enqueue_job(writer: Writer, payment_key: str = "pay_A") -> int:
    async with writer.transaction() as tx:
        return await enqueue(
            tx,
            stage="ledger",
            sink="ledger",
            source="razorpay",
            account="acc_test",
            payment_key=payment_key,
            event_id=f"evt_{payment_key}",
            retry_profile="production",
            now=utc_now(),
        )


async def never_returns(seconds: float) -> None:
    """A heartbeat sleep that never wakes: the worker stalls without heartbeating."""
    await asyncio.Event().wait()


def runner_for(
    writer: Writer,
    work: Work,
    *,
    pauses: Pauses | None = None,
    sleep: Callable[[float], Awaitable[None]] = never_returns,
) -> StageRunner:
    return StageRunner(
        writer,
        stage="ledger",
        sink="ledger",
        limit=1,
        work=work,
        claimed_by=INSTANCE,
        pauses=pauses or Pauses(),
        retry_profile="production",
        rng=random.Random(7),
        sleep=sleep,
    )


async def test_a_fake_stages_job_runs_once_and_ends_done(
    writer: Writer, read_rows: ReadRows
) -> None:
    job_id = await enqueue_job(writer)
    ran: list[int] = []

    async def work(job: ClaimedJob) -> Done:
        ran.append(job.id)
        return Done()

    runner = runner_for(writer, work)
    await runner.run_once()
    await runner.run_once()

    assert ran == [job_id]
    assert await read_rows("SELECT status, attempts FROM jobs") == [
        {"status": "done", "attempts": 1}
    ]
    assert await read_rows("SELECT number, outcome FROM attempts") == [
        {"number": 1, "outcome": "ok"}
    ]


async def test_new_work_during_the_run_sets_rerun_and_the_job_runs_exactly_once_more(
    writer: Writer, read_rows: ReadRows
) -> None:
    await enqueue_job(writer)
    runs = 0

    async def work(job: ClaimedJob) -> Done:
        nonlocal runs
        runs += 1
        if runs == 1:
            await enqueue_job(writer)  # an event for the payment arrives mid-run
        return Done()

    runner = runner_for(writer, work)
    for _ in range(3):
        await runner.run_once()

    assert runs == 2
    assert await read_rows("SELECT status, rerun, attempts FROM jobs") == [
        {"status": "done", "rerun": 0, "attempts": 1}
    ]
    assert await read_rows("SELECT number, outcome FROM attempts ORDER BY id") == [
        {"number": 1, "outcome": "ok"},
        {"number": 1, "outcome": "ok"},
    ]


def failing_with(failure: Failure) -> Work:
    async def work(job: ClaimedJob) -> Done:
        raise StageFailed(failure)

    return work


async def test_a_retry_error_puts_the_job_back_to_waiting_after_its_backoff(
    writer: Writer, read_rows: ReadRows
) -> None:
    await enqueue_job(writer)

    await runner_for(writer, failing_with(Failure("HTTP 503", "retry"))).run_once()

    [job] = await read_rows("SELECT status, attempts, run_after, last_error_class FROM jobs")
    assert (job["status"], job["attempts"], job["last_error_class"]) == ("waiting", 1, "retry")
    # attempt 1's wait is 5 s times 0.5-1.5
    assert "2026-10-02T09:00:02.500000Z" <= job["run_after"] <= "2026-10-02T09:00:07.500000Z"
    assert await read_rows("SELECT number, outcome, error FROM attempts") == [
        {"number": 1, "outcome": "retry", "error": "HTTP 503"}
    ]


async def test_a_retry_after_is_honoured_as_a_minimum(
    writer: Writer, read_rows: ReadRows
) -> None:
    await enqueue_job(writer)
    failure = Failure("HTTP 429", "retry", retry_after=timedelta(hours=1))

    await runner_for(writer, failing_with(failure)).run_once()

    assert await read_rows("SELECT status, run_after FROM jobs") == [
        {"status": "waiting", "run_after": "2026-10-02T10:00:00.000000Z"}
    ]


async def test_a_dead_error_sends_the_job_straight_to_dead(
    writer: Writer, read_rows: ReadRows
) -> None:
    await enqueue_job(writer)

    await runner_for(writer, failing_with(Failure("mapped field missing", "dead"))).run_once()

    assert await read_rows("SELECT status, attempts FROM jobs") == [
        {"status": "dead", "attempts": 1}
    ]
    assert await read_rows("SELECT outcome FROM attempts") == [{"outcome": "dead"}]


async def test_a_pause_error_pauses_the_sink_refunds_the_attempt_and_stops_claims(
    writer: Writer, read_rows: ReadRows
) -> None:
    pauses = Pauses()
    await enqueue_job(writer)
    failure = Failure("spreadsheet not found", "pause", pause_target="sink")
    runner = runner_for(writer, failing_with(failure), pauses=pauses)

    await runner.run_once()
    claimed_while_paused = await runner.run_once()

    assert pauses.sinks == {"ledger"}
    assert claimed_while_paused == 0
    assert await read_rows("SELECT status, attempts FROM jobs") == [
        {"status": "waiting", "attempts": 0}
    ]
    assert await read_rows("SELECT outcome FROM attempts") == [{"outcome": "pause"}]


async def test_an_unexpected_exception_in_the_work_is_our_bug_and_sends_the_job_dead(
    writer: Writer, read_rows: ReadRows
) -> None:
    await enqueue_job(writer)

    async def work(job: ClaimedJob) -> Done:
        raise KeyError("amount")

    await runner_for(writer, work).run_once()

    assert await read_rows("SELECT status, last_error_class FROM jobs") == [
        {"status": "dead", "last_error_class": "dead"}
    ]
    [attempt] = await read_rows("SELECT outcome, error FROM attempts")
    assert attempt["outcome"] == "dead"
    assert "KeyError" in attempt["error"]


async def claim_one(writer: Writer) -> ClaimedJob:
    [job] = await claim(
        writer,
        stage="ledger",
        sink="ledger",
        limit=1,
        claimed_by=INSTANCE,
        pauses=Pauses(),
        now=utc_now(),
    )
    return job


async def reap_now(writer: Writer) -> list[int]:
    return await reap(writer, retry_profile="production", now=utc_now(), rng=random.Random(7))


async def test_the_reaper_returns_an_expired_lease_to_waiting_with_backoff_and_a_reaped_row(
    writer: Writer, read_rows: ReadRows, frozen_clock: TimeMachineFixture
) -> None:
    job_id = await enqueue_job(writer)
    await claim_one(writer)
    frozen_clock.move_to(NINE + timedelta(seconds=59), tick=False)
    assert await reap_now(writer) == []  # the lease still holds
    frozen_clock.move_to(NINE + timedelta(seconds=61), tick=False)

    reaped = await reap_now(writer)

    assert reaped == [job_id]
    [job] = await read_rows("SELECT * FROM jobs")
    assert (job["status"], job["attempts"], job["claim_token"], job["lease_until"]) == (
        "waiting",
        1,
        None,
        None,
    )
    # attempt 1's wait is 5 s times 0.5-1.5, counted from the reap at 09:01:01
    assert "2026-10-02T09:01:03.500000Z" <= job["run_after"] <= "2026-10-02T09:01:08.500000Z"
    assert await read_rows("SELECT job_id, number, finished_at, outcome FROM attempts") == [
        {
            "job_id": job_id,
            "number": 1,
            "finished_at": "2026-10-02T09:01:01.000000Z",
            "outcome": "reaped",
        }
    ]


async def test_the_reaper_sends_a_job_dead_when_its_attempts_are_used_up(
    writer: Writer, read_rows: ReadRows, frozen_clock: TimeMachineFixture
) -> None:
    await enqueue_job(writer)
    for day in range(10):
        frozen_clock.move_to(NINE + timedelta(days=day), tick=False)
        await claim_one(writer)
        frozen_clock.move_to(NINE + timedelta(days=day, minutes=2), tick=False)
        await reap_now(writer)

    assert await read_rows("SELECT status, attempts FROM jobs") == [
        {"status": "dead", "attempts": 10}
    ]
    outcomes = await read_rows("SELECT DISTINCT outcome FROM attempts")
    assert outcomes == [{"outcome": "reaped"}]


async def test_one_row_that_cannot_be_reaped_does_not_stop_the_reaper_reaping_the_others(
    writer: Writer,
    read_rows: ReadRows,
    frozen_clock: TimeMachineFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = await enqueue_job(writer, "pay_A")
    bad = await enqueue_job(writer, "pay_B")
    last = await enqueue_job(writer, "pay_C")
    for _ in range(3):
        await claim_one(writer)
    async with writer.transaction() as tx:  # a row whose every update fails
        await tx.execute(
            "CREATE TRIGGER refuse_pay_b BEFORE UPDATE ON jobs WHEN OLD.payment_key = 'pay_B' "
            "BEGIN SELECT RAISE(ABORT, 'row refuses updates'); END"
        )
    frozen_clock.move_to(NINE + timedelta(seconds=61), tick=False)

    reaped = await reap_now(writer)

    assert reaped == [first, last]
    rows = await read_rows("SELECT id, status FROM jobs ORDER BY id")
    assert rows == [
        {"id": first, "status": "waiting"},
        {"id": bad, "status": "running"},
        {"id": last, "status": "waiting"},
    ]
    assert await read_rows("SELECT job_id FROM attempts ORDER BY id") == [
        {"job_id": first},
        {"job_id": last},
    ]
    [logged] = [r for r in caplog.records if r.getMessage() == "job not reaped"]
    assert logged.__dict__["job_id"] == bad


async def write_stand_in(tx: Transaction) -> None:
    """The stage's own write (a stand-in for the ledger's), committed with `complete`."""
    await tx.execute("CREATE TABLE IF NOT EXISTS ledger_stand_in (written INTEGER)")
    await tx.execute("INSERT INTO ledger_stand_in VALUES (1)")


async def test_a_stage_that_outlives_its_lease_without_heartbeating_is_reaped_and_its_late_completion_discarded(  # noqa: E501
    writer: Writer, read_rows: ReadRows, frozen_clock: TimeMachineFixture
) -> None:
    job_id = await enqueue_job(writer)
    started, finish = asyncio.Event(), asyncio.Event()

    async def stalls(job: ClaimedJob) -> Done:
        started.set()
        await finish.wait()
        return Done(writes=write_stand_in)

    running = asyncio.create_task(runner_for(writer, stalls, sleep=never_returns).run_once())
    await started.wait()
    frozen_clock.move_to(NINE + timedelta(seconds=61), tick=False)
    assert await reap_now(writer) == [job_id]
    finish.set()
    await running

    assert await read_rows("SELECT status, attempts FROM jobs") == [
        {"status": "waiting", "attempts": 1}
    ]
    assert await read_rows("SELECT outcome FROM attempts") == [{"outcome": "reaped"}]
    tables = await read_rows("SELECT name FROM sqlite_master WHERE name = 'ledger_stand_in'")
    assert tables == []


async def test_a_stage_working_past_60_seconds_keeps_its_job_by_heartbeating_every_20(
    writer: Writer, read_rows: ReadRows, frozen_clock: TimeMachineFixture
) -> None:
    await enqueue_job(writer)
    slept: list[float] = []

    async def time_passes(seconds: float) -> None:
        slept.append(seconds)
        frozen_clock.shift(timedelta(seconds=seconds))
        await asyncio.sleep(0)

    async def slow(job: ClaimedJob) -> Done:
        while utc_now() < NINE + timedelta(seconds=90):
            await asyncio.sleep(0)
        assert await reap_now(writer) == []
        return Done()

    await asyncio.wait_for(runner_for(writer, slow, sleep=time_passes).run_once(), timeout=5)

    assert set(slept) == {20}
    assert await read_rows("SELECT status, attempts FROM jobs") == [
        {"status": "done", "attempts": 1}
    ]
    assert await read_rows("SELECT outcome FROM attempts") == [{"outcome": "ok"}]


async def test_a_heartbeat_that_finds_the_lease_lost_stops_the_work_without_writing(
    writer: Writer, read_rows: ReadRows, frozen_clock: TimeMachineFixture
) -> None:
    job_id = await enqueue_job(writer)
    started, reaped = asyncio.Event(), asyncio.Event()

    async def after_the_reap(seconds: float) -> None:
        await reaped.wait()

    async def never_finishes(job: ClaimedJob) -> Done:
        started.set()
        await asyncio.Event().wait()
        return Done(writes=write_stand_in)

    runner = runner_for(writer, never_finishes, sleep=after_the_reap)
    running = asyncio.create_task(runner.run_once())
    await started.wait()
    frozen_clock.move_to(NINE + timedelta(seconds=61), tick=False)
    assert await reap_now(writer) == [job_id]
    reaped.set()
    await asyncio.wait_for(running, timeout=5)

    assert await read_rows("SELECT status, attempts FROM jobs") == [
        {"status": "waiting", "attempts": 1}
    ]
    assert await read_rows("SELECT outcome FROM attempts") == [{"outcome": "reaped"}]


async def test_the_reaper_runs_every_30_seconds_and_takes_back_a_lease_once_it_expires(
    writer: Writer, read_rows: ReadRows, frozen_clock: TimeMachineFixture
) -> None:
    await enqueue_job(writer)
    await claim_one(writer)  # its lease runs to 09:01:00
    slept: list[float] = []
    fourth_pass_done = asyncio.Event()

    async def time_passes(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 4:
            fourth_pass_done.set()
            await asyncio.Event().wait()
        frozen_clock.shift(timedelta(seconds=seconds))

    reaper = asyncio.create_task(
        run_reaper(writer, retry_profile="production", rng=random.Random(7), sleep=time_passes)
    )
    await asyncio.wait_for(fourth_pass_done.wait(), timeout=5)
    reaper.cancel()

    assert slept == [30, 30, 30, 30]
    assert await read_rows("SELECT status FROM jobs") == [{"status": "waiting"}]
    assert await read_rows("SELECT finished_at, outcome FROM attempts") == [
        {"finished_at": "2026-10-02T09:01:30.000000Z", "outcome": "reaped"}
    ]


async def test_a_stopped_runner_finishes_the_job_it_holds_and_claims_no_more(
    writer: Writer, read_rows: ReadRows
) -> None:
    await enqueue_job(writer, "pay_A")
    await enqueue_job(writer, "pay_B")
    runner: StageRunner

    async def stops_the_runner(job: ClaimedJob) -> Done:
        runner.stop()
        return Done()

    runner = runner_for(writer, stops_the_runner)
    await asyncio.wait_for(runner.run(), timeout=5)

    rows = await read_rows("SELECT payment_key, status FROM jobs ORDER BY id")
    assert rows == [
        {"payment_key": "pay_A", "status": "done"},
        {"payment_key": "pay_B", "status": "waiting"},
    ]


async def test_a_running_runner_waits_when_nothing_is_due_then_claims_new_work(
    writer: Writer, read_rows: ReadRows
) -> None:
    waits: list[float] = []
    runner: StageRunner

    async def idle(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 1:
            await enqueue_job(writer)

    async def work(job: ClaimedJob) -> Done:
        runner.stop()
        return Done()

    runner = runner_for(writer, work, sleep=idle)
    await asyncio.wait_for(runner.run(), timeout=5)

    assert waits[0] == 1
    assert await read_rows("SELECT status FROM jobs") == [{"status": "done"}]
