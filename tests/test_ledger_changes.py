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
from webhook_sync.queue import Pauses, StageRunner, enqueue
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
