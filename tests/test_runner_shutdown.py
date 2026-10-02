"""Stopping the stage runners at shutdown, with a fake stage (seam 2: the queue).

Real SQLite in a fresh data directory, time frozen with time-machine. The 20 s grace is the
injected sleep, so a test decides when it is over instead of waiting for it.
"""

import asyncio
import random
from datetime import UTC, datetime

import pytest
from support import ReadRows
from time_machine import TimeMachineFixture

from webhook_sync.clock import utc_now
from webhook_sync.db.writer import Writer
from webhook_sync.queue import ClaimedJob, Done, Pauses, Runners, StageRunner, Work, enqueue

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
    await asyncio.Event().wait()


def runner_for(writer: Writer, work: Work) -> StageRunner:
    return StageRunner(
        writer,
        stage="ledger",
        sink="ledger",
        limit=1,
        work=work,
        claimed_by=INSTANCE,
        pauses=Pauses(),
        retry_profile="production",
        rng=random.Random(7),
        sleep=never_returns,
    )


async def test_a_job_that_finishes_within_the_grace_completes_and_nothing_is_released(
    writer: Writer, read_rows: ReadRows
) -> None:
    await enqueue_job(writer)
    started, finish = asyncio.Event(), asyncio.Event()

    async def work(job: ClaimedJob) -> Done:
        started.set()
        await finish.wait()
        return Done()

    runners = Runners(writer, [runner_for(writer, work)], sleep=never_returns)
    runners.start()
    await started.wait()
    stopping = asyncio.create_task(runners.stop())
    await asyncio.sleep(0)  # the stop has begun: no more claims
    finish.set()
    released = await asyncio.wait_for(stopping, timeout=5)

    assert released == []
    assert await read_rows("SELECT status, attempts FROM jobs") == [
        {"status": "done", "attempts": 1}
    ]
    assert await read_rows("SELECT outcome FROM attempts") == [{"outcome": "ok"}]


async def test_a_job_still_running_after_20_seconds_is_released_with_its_attempt_refunded(
    writer: Writer, read_rows: ReadRows
) -> None:
    job_id = await enqueue_job(writer)
    started = asyncio.Event()
    graces: list[float] = []

    async def stuck(job: ClaimedJob) -> Done:
        started.set()
        await asyncio.Event().wait()
        return Done()

    async def grace_over(seconds: float) -> None:
        graces.append(seconds)

    runners = Runners(writer, [runner_for(writer, stuck)], sleep=grace_over)
    runners.start()
    await started.wait()
    released = await asyncio.wait_for(runners.stop(), timeout=5)

    assert graces == [20]
    assert released == [job_id]
    assert await read_rows(
        "SELECT status, attempts, claim_token, lease_until, run_after FROM jobs"
    ) == [
        {
            "status": "waiting",
            "attempts": 0,
            "claim_token": None,
            "lease_until": None,
            "run_after": "2026-10-02T09:00:00.000000Z",
        }
    ]
    assert await read_rows("SELECT number, outcome, finished_at FROM attempts") == [
        {"number": 1, "outcome": "released", "finished_at": "2026-10-02T09:00:00.000000Z"}
    ]
