"""The stage runner and the reaper, with a fake stage (seam 2: the queue).

Real SQLite in a fresh data directory, time moved with time-machine and never slept: the runner
is driven one pass at a time, and its heartbeat's sleep is injected. Assertions are on the
`jobs` and `attempts` rows.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from support import ReadRows
from time_machine import TimeMachineFixture

from webhook_sync.clock import utc_now
from webhook_sync.db.writer import Writer
from webhook_sync.queue import ClaimedJob, Done, Pauses, StageRunner, Work, enqueue

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
