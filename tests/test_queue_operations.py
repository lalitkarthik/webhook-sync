"""The queue's operations beyond enqueue: claim, heartbeat, complete, fail (seam 2: the queue).

Real SQLite in a fresh data directory, time moved with time-machine, the jitter's random source
injected. Assertions are on the returned values and the `jobs` and `attempts` rows.
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from support import ReadRows
from time_machine import TimeMachineFixture

from webhook_sync.clock import utc_now
from webhook_sync.db.writer import Writer
from webhook_sync.queue import LeaseLost, Pauses, claim, complete, enqueue, heartbeat

NINE = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
INSTANCE = "wsync-test@2026-10-02T08:59:00.000000Z"


@pytest.fixture(autouse=True)
def frozen_clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(NINE, tick=False)
    return time_machine


async def enqueue_job(writer: Writer, payment_key: str = "pay_A", *, sink: str = "ledger") -> int:
    async with writer.transaction() as tx:
        return await enqueue(
            tx,
            stage="ledger",
            sink=sink,
            source="razorpay",
            account="acc_test",
            payment_key=payment_key,
            event_id=f"evt_{payment_key}",
            retry_profile="production",
            now=utc_now(),
        )


async def claim_ledger(writer: Writer, *, limit: int = 1, pauses: Pauses | None = None):
    return await claim(
        writer,
        stage="ledger",
        sink="ledger",
        limit=limit,
        claimed_by=INSTANCE,
        pauses=pauses or Pauses(),
        now=utc_now(),
    )


async def test_claim_hands_out_a_due_job_with_a_60_second_lease_and_a_claim_token(
    writer: Writer, read_rows: ReadRows
) -> None:
    job_id = await enqueue_job(writer)

    [claimed] = await claim_ledger(writer)

    assert claimed.id == job_id
    assert claimed.payment_key == "pay_A"
    assert claimed.attempts == 1
    assert claimed.claim_token
    [job] = await read_rows("SELECT * FROM jobs")
    assert (job["status"], job["attempts"], job["claim_token"]) == (
        "running",
        1,
        claimed.claim_token,
    )
    assert job["lease_until"] == "2026-10-02T09:01:00.000000Z"
    assert job["claimed_by"] == INSTANCE
    assert job["updated_at"] == "2026-10-02T09:00:00.000000Z"


async def test_claim_takes_due_jobs_oldest_first_up_to_the_limit(
    writer: Writer, frozen_clock: TimeMachineFixture
) -> None:
    first = await enqueue_job(writer, "pay_A")
    frozen_clock.move_to(NINE + timedelta(seconds=1), tick=False)
    second = await enqueue_job(writer, "pay_B")
    await enqueue_job(writer, "pay_C")
    await enqueue_job(writer, "pay_D", sink="sheets")

    claimed = await claim_ledger(writer, limit=2)

    assert [job.id for job in claimed] == [first, second]
    assert len({job.claim_token for job in claimed}) == 1
    assert await claim_ledger(writer, limit=5) != []  # pay_C is still due
    assert await claim_ledger(writer, limit=5) == []  # running jobs are not claimed again


async def test_claim_takes_no_job_before_its_run_after(writer: Writer) -> None:
    await enqueue_job(writer)
    async with writer.transaction() as tx:
        await tx.execute("UPDATE jobs SET run_after = '2026-10-02T09:00:00.000001Z'")

    assert await claim_ledger(writer) == []


async def test_a_heartbeat_pushes_the_lease_60_seconds_past_now(
    writer: Writer, read_rows: ReadRows, frozen_clock: TimeMachineFixture
) -> None:
    await enqueue_job(writer)
    [claimed] = await claim_ledger(writer)
    frozen_clock.move_to(NINE + timedelta(seconds=20), tick=False)

    lost = await heartbeat(writer, [claimed.id], claimed.claim_token, now=utc_now())

    assert lost == set()
    assert await read_rows("SELECT lease_until FROM jobs") == [
        {"lease_until": "2026-10-02T09:01:20.000000Z"}
    ]


async def test_a_heartbeat_with_a_lost_lease_reports_it_and_extends_nothing(
    writer: Writer, read_rows: ReadRows
) -> None:
    await enqueue_job(writer, "pay_A")
    await enqueue_job(writer, "pay_B")
    held, taken = await claim_ledger(writer, limit=2)

    lost = await heartbeat(writer, [held.id, taken.id], "stale-token", now=utc_now())

    assert lost == {held.id, taken.id}
    leases = await read_rows("SELECT DISTINCT lease_until FROM jobs")
    assert leases == [{"lease_until": "2026-10-02T09:01:00.000000Z"}]


async def test_complete_marks_the_job_done_and_records_an_ok_attempt(
    writer: Writer, read_rows: ReadRows, frozen_clock: TimeMachineFixture
) -> None:
    job_id = await enqueue_job(writer)
    [claimed] = await claim_ledger(writer)
    frozen_clock.move_to(NINE + timedelta(seconds=2), tick=False)

    async with writer.transaction() as tx:
        await complete(tx, claimed, now=utc_now())

    [job] = await read_rows("SELECT * FROM jobs")
    assert (job["status"], job["attempts"], job["claim_token"], job["lease_until"]) == (
        "done",
        1,
        None,
        None,
    )
    assert await read_rows("SELECT * FROM attempts") == [
        {
            "id": 1,
            "job_id": job_id,
            "number": 1,
            "started_at": "2026-10-02T09:00:00.000000Z",
            "finished_at": "2026-10-02T09:00:02.000000Z",
            "outcome": "ok",
            "error": None,
            "duration_ms": 2000,
        }
    ]


async def test_complete_with_rerun_set_puts_the_job_straight_back_to_waiting(
    writer: Writer, read_rows: ReadRows, frozen_clock: TimeMachineFixture
) -> None:
    await enqueue_job(writer)
    [claimed] = await claim_ledger(writer)
    await enqueue_job(writer)  # new work while the job runs sets rerun
    frozen_clock.move_to(NINE + timedelta(seconds=3), tick=False)

    async with writer.transaction() as tx:
        await complete(tx, claimed, now=utc_now())

    [job] = await read_rows("SELECT * FROM jobs")
    assert (job["status"], job["rerun"], job["attempts"], job["claim_token"]) == (
        "waiting",
        0,
        0,
        None,
    )
    assert job["run_after"] == "2026-10-02T09:00:03.000000Z"
    [again] = await claim_ledger(writer)
    assert again.id == claimed.id


async def test_complete_with_a_stale_token_matches_nothing_and_rolls_back_the_callers_writes(
    writer: Writer, read_rows: ReadRows
) -> None:
    await enqueue_job(writer)
    [claimed] = await claim_ledger(writer)
    stale = replace(claimed, claim_token="stale-token")

    with pytest.raises(LeaseLost):
        async with writer.transaction() as tx:
            await tx.execute("CREATE TABLE the_works_write (x INTEGER)")  # a stand-in ledger write
            await complete(tx, stale, now=utc_now())

    tables = await read_rows("SELECT name FROM sqlite_master WHERE name = 'the_works_write'")
    assert tables == []
    [job] = await read_rows("SELECT status, claim_token FROM jobs")
    assert job == {"status": "running", "claim_token": claimed.claim_token}
    assert await read_rows("SELECT * FROM attempts") == []
