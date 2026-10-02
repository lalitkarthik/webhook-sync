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
from datetime import UTC, datetime
from typing import Any

import pytest
import respx
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
