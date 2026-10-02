"""Only a real change reaches the sheet (seam: the ledger stage on P1's stage runner).

A fetch whose row hash differs upserts the row and enqueues one mirror job per configured sink
in the same transaction as the ledger job's `complete`; an unchanged fetch writes nothing.
Events folded into one live job cost one fetch; an event during a fetch costs one more; a
worker whose lease was taken over has its writes rolled back. Jobs come from `enqueue`,
Razorpay's API is answered by respx with recorded test-mode bodies, time by time-machine.
"""

import asyncio
import random
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
import sqlalchemy as sa
from support import (
    RECORDED_ACCOUNT,
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_WEBHOOK_SECRET,
    ReadRows,
    recorded_api_answer,
    recorded_fetch,
)
from time_machine import TimeMachineFixture

from webhook_sync.clock import utc_now
from webhook_sync.db.schema import jobs
from webhook_sync.db.writer import Writer
from webhook_sync.ledger import ledger_runner
from webhook_sync.queue import Pauses, StageRunner, enqueue, reap
from webhook_sync.sources.razorpay import RazorpayAdapter, api_client

RAZORPAY_API = "https://api.razorpay.test"
NINE = datetime(2026, 10, 2, 20, 30, tzinfo=UTC)
PAYMENT = "pay_Tj9rKBOXWcsMeE"
FETCH_PATH = f"/v1/payments/{PAYMENT}"


@pytest.fixture(autouse=True)
def frozen_clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(NINE, tick=False)
    return time_machine


@pytest.fixture
def razorpay() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=RAZORPAY_API, assert_all_called=False) as router:
        yield router


def authorized() -> dict[str, Any]:
    return recorded_api_answer(f"20261003T010947-{PAYMENT}-authorized")


def captured() -> dict[str, Any]:
    return recorded_fetch(f"payment-captured-{PAYMENT}")


async def never_wakes(seconds: float) -> None:
    """The runner's sleep: no heartbeat, no idle wait; the test drives one pass at a time."""
    await asyncio.Event().wait()


@pytest.fixture
async def adapter() -> AsyncIterator[RazorpayAdapter]:
    async with api_client(RAZORPAY_API, TEST_KEY_ID, TEST_KEY_SECRET) as api:
        yield RazorpayAdapter(
            mode="test",
            account=RECORDED_ACCOUNT,
            webhook_secrets=(TEST_WEBHOOK_SECRET,),
            api=api,
        )


def make_ledger(writer: Writer, adapter: RazorpayAdapter) -> StageRunner:
    """A ledger stage on the stage runner, mirroring to the configured sink `sheets`."""
    return ledger_runner(
        writer,
        {"razorpay": adapter},
        mirror_sinks=("sheets",),
        claimed_by="wsync-test",
        pauses=Pauses(),
        retry_profile="production",
        rng=random.Random(7),
        sleep=never_wakes,
    )


@pytest.fixture
def ledger(writer: Writer, adapter: RazorpayAdapter) -> StageRunner:
    return make_ledger(writer, adapter)


async def enqueue_ledger_job(writer: Writer, event_id: str | None = None) -> int:
    async with writer.transaction() as tx:
        return await enqueue(
            tx,
            stage="ledger",
            sink="ledger",
            source="razorpay",
            account=RECORDED_ACCOUNT,
            payment_key=PAYMENT,
            event_id=event_id,
            retry_profile="production",
            now=utc_now(),
        )


MIRROR_JOBS = (
    "SELECT stage, sink, source, account, payment_key, status, caused_by_event_id"
    " FROM jobs WHERE stage = 'mirror' ORDER BY id"
)


def waiting_mirror_job(event_id: str | None) -> dict[str, Any]:
    return {
        "stage": "mirror",
        "sink": "sheets",
        "source": "razorpay",
        "account": RECORDED_ACCOUNT,
        "payment_key": PAYMENT,
        "status": "waiting",
        "caused_by_event_id": event_id,
    }


async def test_a_new_row_makes_one_waiting_mirror_job_for_the_sheet(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    razorpay.get(FETCH_PATH).respond(200, json=captured())
    await enqueue_ledger_job(writer, "evt_first")

    await ledger.run_once()

    assert await read_rows("SELECT row_version FROM payments") == [{"row_version": 1}]
    assert await read_rows(MIRROR_JOBS) == [waiting_mirror_job("evt_first")]


async def test_a_changed_fetch_bumps_the_version_and_leaves_one_waiting_mirror_job(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    frozen_clock: TimeMachineFixture,
) -> None:
    razorpay.get(FETCH_PATH).mock(
        side_effect=[httpx.Response(200, json=authorized()), httpx.Response(200, json=captured())]
    )
    await enqueue_ledger_job(writer, "evt_authorized")
    await ledger.run_once()
    frozen_clock.shift(timedelta(minutes=1))
    await enqueue_ledger_job(writer, "evt_captured")

    await ledger.run_once()

    assert await read_rows(
        "SELECT status, row_version, last_event_id, updated_at FROM payments"
    ) == [
        {
            "status": "succeeded",
            "row_version": 2,
            "last_event_id": "evt_captured",
            "updated_at": "2026-10-02T20:31:00.000000Z",
        }
    ]
    # The flusher hasn't taken the first change yet: the second folds into its live job.
    assert await read_rows(MIRROR_JOBS) == [waiting_mirror_job("evt_captured")]


async def flusher_takes_the_mirror_jobs(writer: Writer) -> None:
    """Stand-in for P3's flusher: every mirror job is done, so none is live any more."""
    async with writer.transaction() as tx:
        await tx.execute(sa.update(jobs).where(jobs.c.stage == "mirror").values(status="done"))


async def test_an_unchanged_fetch_writes_nothing_and_makes_no_mirror_job(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    frozen_clock: TimeMachineFixture,
) -> None:
    fetch = razorpay.get(FETCH_PATH).respond(200, json=captured())
    await enqueue_ledger_job(writer, "evt_first")
    await ledger.run_once()
    await flusher_takes_the_mirror_jobs(writer)
    before = await read_rows("SELECT * FROM payments")
    frozen_clock.shift(timedelta(minutes=5))
    await enqueue_ledger_job(writer, "evt_replayed")

    await ledger.run_once()

    assert fetch.call_count == 2
    assert await read_rows("SELECT * FROM payments") == before
    assert before[0]["last_event_id"] == "evt_first"
    assert await read_rows("SELECT status FROM jobs WHERE stage = 'mirror'") == [{"status": "done"}]
    assert await read_rows("SELECT status FROM jobs WHERE stage = 'ledger'") == [
        {"status": "done"},
        {"status": "done"},
    ]


async def test_many_events_while_the_job_waits_cost_one_fetch(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    fetch = razorpay.get(FETCH_PATH).respond(200, json=captured())
    for event_id in ("evt_1", "evt_2", "evt_3"):
        await enqueue_ledger_job(writer, event_id)

    while await ledger.run_once():
        pass

    assert fetch.call_count == 1
    assert await read_rows("SELECT status FROM jobs WHERE stage = 'ledger'") == [{"status": "done"}]
    assert await read_rows("SELECT row_version, last_event_id FROM payments") == [
        {"row_version": 1, "last_event_id": "evt_3"}
    ]


async def test_an_event_during_a_fetch_causes_one_more_fetch_and_the_row_ends_at_its_answer(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    fetching = asyncio.Event()
    answered = asyncio.Event()
    answers = [authorized(), captured()]

    async def razorpay_answers(request: httpx.Request) -> httpx.Response:
        if len(answers) == 2:  # the first fetch waits until the new event is in
            fetching.set()
            await answered.wait()
        return httpx.Response(200, json=answers.pop(0))

    fetch = razorpay.get(FETCH_PATH).mock(side_effect=razorpay_answers)
    await enqueue_ledger_job(writer, "evt_authorized")
    first = asyncio.create_task(ledger.run_once())
    await asyncio.wait_for(fetching.wait(), timeout=5)
    await enqueue_ledger_job(writer, "evt_captured")
    answered.set()
    await asyncio.wait_for(first, timeout=5)

    assert await read_rows("SELECT status, rerun FROM jobs WHERE stage = 'ledger'") == [
        {"status": "waiting", "rerun": 0}
    ]
    while await ledger.run_once():
        pass

    assert fetch.call_count == 2
    assert await read_rows("SELECT status FROM jobs WHERE stage = 'ledger'") == [{"status": "done"}]
    assert await read_rows(
        "SELECT status, source_status, row_version, last_event_id FROM payments"
    ) == [
        {
            "status": "succeeded",
            "source_status": "captured",
            "row_version": 2,
            "last_event_id": "evt_captured",
        }
    ]
    assert await read_rows(MIRROR_JOBS) == [waiting_mirror_job("evt_captured")]


async def test_a_worker_that_lost_its_lease_mid_fetch_has_its_writes_rolled_back(
    writer: Writer,
    adapter: RazorpayAdapter,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    frozen_clock: TimeMachineFixture,
) -> None:
    stalled, new_owner = make_ledger(writer, adapter), make_ledger(writer, adapter)
    fetching = asyncio.Event()
    answered = asyncio.Event()
    calls = 0

    async def razorpay_answers(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:  # the stalled worker's fetch hangs, then brings back an older state
            fetching.set()
            await answered.wait()
            return httpx.Response(200, json=authorized())
        return httpx.Response(200, json=captured())

    razorpay.get(FETCH_PATH).mock(side_effect=razorpay_answers)
    await enqueue_ledger_job(writer, "evt_captured")
    stale = asyncio.create_task(stalled.run_once())
    await asyncio.wait_for(fetching.wait(), timeout=5)

    # The lease runs out; the reaper takes the job back and the new owner runs it after backoff.
    frozen_clock.shift(timedelta(seconds=61))
    await reap(writer, retry_profile="production", now=utc_now(), rng=random.Random(7))
    frozen_clock.shift(timedelta(seconds=10))
    assert await new_owner.run_once() == 1
    taken_over_at = "2026-10-02T20:31:11.000000Z"

    # The stalled fetch finally answers; its upsert, mirror job and complete roll back.
    frozen_clock.shift(timedelta(minutes=1))
    answered.set()
    await asyncio.wait_for(stale, timeout=5)

    assert calls == 2
    assert await read_rows(
        "SELECT status, source_status, row_version, last_event_id, updated_at FROM payments"
    ) == [
        {
            "status": "succeeded",
            "source_status": "captured",
            "row_version": 1,
            "last_event_id": "evt_captured",
            "updated_at": taken_over_at,
        }
    ]
    assert await read_rows("SELECT stage, sink, status, updated_at FROM jobs ORDER BY id") == [
        {"stage": "ledger", "sink": "ledger", "status": "done", "updated_at": taken_over_at},
        {"stage": "mirror", "sink": "sheets", "status": "waiting", "updated_at": taken_over_at},
    ]
    assert await read_rows("SELECT number, outcome FROM attempts ORDER BY id") == [
        {"number": 1, "outcome": "reaped"},
        {"number": 2, "outcome": "ok"},
    ]


async def test_a_change_in_a_column_the_sheet_may_not_show_still_makes_a_mirror_job(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    frozen_clock: TimeMachineFixture,
) -> None:
    # Only the GST on the fee changes (fee and net stay): a sheet configured without its column
    # shows nothing new, but the hash covers every business column, not the shown ones (A3).
    retaxed = {**captured(), "tax": 0}
    razorpay.get(FETCH_PATH).mock(
        side_effect=[httpx.Response(200, json=captured()), httpx.Response(200, json=retaxed)]
    )
    await enqueue_ledger_job(writer, "evt_captured")
    await ledger.run_once()
    await flusher_takes_the_mirror_jobs(writer)
    frozen_clock.shift(timedelta(minutes=1))
    await enqueue_ledger_job(writer, "evt_updated")

    await ledger.run_once()

    assert await read_rows(
        "SELECT fee_minor, fee_tax_minor, net_minor, row_version FROM payments"
    ) == [{"fee_minor": 1180, "fee_tax_minor": 0, "net_minor": 48820, "row_version": 2}]
    assert await read_rows(MIRROR_JOBS) == [
        {**waiting_mirror_job("evt_captured"), "status": "done"},
        waiting_mirror_job("evt_updated"),
    ]
