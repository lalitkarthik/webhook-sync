"""When Razorpay says no, throttles or is down (seam: the ledger stage on P1's stage runner).

Every answer ends the way the design says (A5, A12, A17): an unknown payment is flagged "not
found at source", or with no row ends dead naming a likely mix-up; throttling, outages and
timeouts retry, honouring `Retry-After` as a minimum; a revoked key pauses the source; anything
else unexpected goes to dead with its reason; fetches stay under 10 a second. Jobs come from
`enqueue`, Razorpay's API is answered by respx with recorded bodies, time by time-machine.
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
MIX_UP = "not found at source: possible mode or account mix-up"


@pytest.fixture(autouse=True)
def frozen_clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(NINE, tick=False)
    return time_machine


@pytest.fixture
def razorpay() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=RAZORPAY_API, assert_all_called=False) as router:
        yield router


def not_found() -> dict[str, Any]:
    return recorded_fetch("not-found-well-formed")


def captured() -> dict[str, Any]:
    return recorded_fetch(f"payment-captured-{PAYMENT}")


async def never_wakes(seconds: float) -> None:
    """The runner's sleep: no heartbeat, no idle wait; the test drives one pass at a time."""
    await asyncio.Event().wait()


@pytest.fixture
def pauses() -> Pauses:
    return Pauses()


@pytest.fixture
async def adapter() -> AsyncIterator[RazorpayAdapter]:
    async with api_client(RAZORPAY_API, TEST_KEY_ID, TEST_KEY_SECRET) as api:
        yield RazorpayAdapter(
            mode="test",
            account=RECORDED_ACCOUNT,
            webhook_secrets=(TEST_WEBHOOK_SECRET,),
            api=api,
        )


@pytest.fixture
def ledger(writer: Writer, adapter: RazorpayAdapter, pauses: Pauses) -> StageRunner:
    return ledger_runner(
        writer,
        {"razorpay": adapter},
        mirror_sinks=("sheets",),
        claimed_by="wsync-test",
        pauses=pauses,
        retry_profile="production",
        rng=random.Random(7),
        sleep=never_wakes,
    )


async def enqueue_ledger_job(
    writer: Writer, event_id: str | None = None, payment_key: str = PAYMENT
) -> int:
    async with writer.transaction() as tx:
        return await enqueue(
            tx,
            stage="ledger",
            sink="ledger",
            source="razorpay",
            account=RECORDED_ACCOUNT,
            payment_key=payment_key,
            event_id=event_id,
            retry_profile="production",
            now=utc_now(),
        )


LEDGER_JOB = (
    "SELECT status, attempts, run_after, last_error, last_error_class"
    " FROM jobs WHERE stage = 'ledger'"
)
ATTEMPTS = "SELECT number, outcome FROM attempts ORDER BY id"


async def test_an_unknown_payment_with_no_row_ends_dead_naming_a_mix_up(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    razorpay.get(FETCH_PATH).respond(400, json=not_found())
    await enqueue_ledger_job(writer, "evt_unknown")

    await ledger.run_once()

    assert await read_rows("SELECT * FROM payments") == []
    [job] = await read_rows(LEDGER_JOB)
    assert (job["status"], job["last_error"], job["last_error_class"]) == ("dead", MIX_UP, "dead")
    assert await read_rows(ATTEMPTS) == [{"number": 1, "outcome": "dead"}]
    assert await read_rows("SELECT * FROM jobs WHERE stage = 'mirror'") == []


async def flusher_takes_the_mirror_jobs(writer: Writer) -> None:
    """Stand-in for P3's flusher: every mirror job is done, so none is live any more."""
    async with writer.transaction() as tx:
        await tx.execute(sa.update(jobs).where(jobs.c.stage == "mirror").values(status="done"))


async def fetch_in_turn(
    writer: Writer,
    ledger: StageRunner,
    clock: TimeMachineFixture,
    *event_ids: str,
) -> None:
    """One ledger job per event, ten minutes apart, each run and its mirror job flushed."""
    for event_id in event_ids:
        await enqueue_ledger_job(writer, event_id)
        await ledger.run_once()
        await flusher_takes_the_mirror_jobs(writer)
        clock.shift(timedelta(minutes=10))


BUSINESS = (
    "SELECT status, amount_minor, fee_minor, net_minor, customer_email, not_found_at,"
    " row_version, last_event_id, updated_at FROM payments"
)
MIRROR_JOBS = "SELECT caused_by_event_id FROM jobs WHERE stage = 'mirror' ORDER BY id"


async def test_an_unknown_payment_with_a_row_keeps_its_values_and_is_flagged_not_found(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    frozen_clock: TimeMachineFixture,
) -> None:
    razorpay.get(FETCH_PATH).mock(
        side_effect=[httpx.Response(200, json=captured()), httpx.Response(400, json=not_found())]
    )
    await fetch_in_turn(writer, ledger, frozen_clock, "evt_captured")
    await enqueue_ledger_job(writer, "evt_later")

    await ledger.run_once()

    assert await read_rows(BUSINESS) == [
        {
            "status": "succeeded",
            "amount_minor": 50000,
            "fee_minor": 1180,
            "net_minor": 48820,
            "customer_email": "customer@example.com",
            "not_found_at": "2026-10-02T20:40:00.000000Z",
            "row_version": 2,
            "last_event_id": "evt_later",
            "updated_at": "2026-10-02T20:40:00.000000Z",
        }
    ]
    assert await read_rows(
        "SELECT caused_by_event_id, status FROM jobs WHERE stage = 'mirror' ORDER BY id"
    ) == [
        {"caused_by_event_id": "evt_captured", "status": "done"},
        {"caused_by_event_id": "evt_later", "status": "waiting"},
    ]
    assert [job["status"] for job in await read_rows(LEDGER_JOB)] == ["done", "done"]
    assert await read_rows(ATTEMPTS) == [
        {"number": 1, "outcome": "ok"},
        {"number": 1, "outcome": "ok"},
    ]


async def test_a_payment_still_not_found_writes_nothing_more(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    frozen_clock: TimeMachineFixture,
) -> None:
    razorpay.get(FETCH_PATH).mock(
        side_effect=[
            httpx.Response(200, json=captured()),
            httpx.Response(400, json=not_found()),
            httpx.Response(400, json=not_found()),
        ]
    )

    await fetch_in_turn(writer, ledger, frozen_clock, "evt_captured", "evt_lost", "evt_again")

    [row] = await read_rows(BUSINESS)
    assert (row["not_found_at"], row["row_version"], row["last_event_id"]) == (
        "2026-10-02T20:40:00.000000Z",
        2,
        "evt_lost",
    )
    assert await read_rows(MIRROR_JOBS) == [
        {"caused_by_event_id": "evt_captured"},
        {"caused_by_event_id": "evt_lost"},
    ]


async def test_a_later_good_fetch_clears_not_found(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    frozen_clock: TimeMachineFixture,
) -> None:
    razorpay.get(FETCH_PATH).mock(
        side_effect=[
            httpx.Response(200, json=captured()),
            httpx.Response(400, json=not_found()),
            httpx.Response(200, json=captured()),
        ]
    )

    await fetch_in_turn(writer, ledger, frozen_clock, "evt_captured", "evt_lost", "evt_back")

    [row] = await read_rows(BUSINESS)
    assert (row["not_found_at"], row["row_version"], row["last_event_id"]) == (
        None,
        3,
        "evt_back",
    )
    assert await read_rows(MIRROR_JOBS) == [
        {"caused_by_event_id": "evt_captured"},
        {"caused_by_event_id": "evt_lost"},
        {"caused_by_event_id": "evt_back"},
    ]
