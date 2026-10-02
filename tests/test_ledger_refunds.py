"""Refunds: the status and refunded amount of refunded, partly refunded and canceled payments.

Seam: the ledger stage on P1's stage runner, its jobs created by `enqueue`, Razorpay's API
answered by respx with the recorded bodies (`tests/recordings/razorpay/fetch/`). `refunded_minor`
is the sum of the processed refunds; pending and failed ones count for nothing (A6, G65); an
uncaptured payment Razorpay auto-refunded is canceled (A7).
"""

import asyncio
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
PARTLY_REFUNDED = "pay_Tj9gmEfed6kRqO"


@pytest.fixture(autouse=True)
def frozen_clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(NINE, tick=False)
    return time_machine


@pytest.fixture
def razorpay() -> Iterator[respx.MockRouter]:
    """Razorpay's API, answered by respx; a request no route matches fails the test."""
    with respx.mock(base_url=RAZORPAY_API, assert_all_called=False) as router:
        yield router


async def never_wakes(seconds: float) -> None:
    """The runner's sleep: no heartbeat, no idle wait; the test drives one pass at a time."""
    await asyncio.Event().wait()


@pytest.fixture
async def ledger(writer: Writer) -> AsyncIterator[StageRunner]:
    """The ledger stage on the stage runner, fetching from the respx-faked API."""
    async with api_client(RAZORPAY_API, TEST_KEY_ID, TEST_KEY_SECRET) as api:
        adapter = RazorpayAdapter(
            mode="test",
            account=RECORDED_ACCOUNT,
            webhook_secrets=(TEST_WEBHOOK_SECRET,),
            api=api,
        )
        yield ledger_runner(
            writer,
            {"razorpay": adapter},
            claimed_by="wsync-test",
            pauses=Pauses(),
            retry_profile="production",
            sleep=never_wakes,
        )


async def enqueue_ledger_job(writer: Writer, payment_key: str) -> int:
    async with writer.transaction() as tx:
        return await enqueue(
            tx,
            stage="ledger",
            sink="ledger",
            source="razorpay",
            account=RECORDED_ACCOUNT,
            payment_key=payment_key,
            event_id=None,
            retry_profile="production",
            now=utc_now(),
        )


async def ledger_row_after_fetch(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Fetch `state` once through the ledger stage; the payment's status and refunded amount."""
    razorpay.get(f"/v1/payments/{state['id']}").respond(200, json=state)
    await enqueue_ledger_job(writer, state["id"])
    await ledger.run_once()
    [row] = await read_rows("SELECT status, refunded_minor FROM payments")
    return row


async def test_a_partly_refunded_payment_is_partially_refunded_by_its_processed_refunds(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    state = recorded_fetch(f"payment-captured-partly-refunded-{PARTLY_REFUNDED}")

    row = await ledger_row_after_fetch(writer, ledger, razorpay, read_rows, state)

    assert row == {"status": "partially_refunded", "refunded_minor": 20000}
    assert await read_rows("SELECT status FROM jobs") == [{"status": "done"}]


def with_refunds(state: dict[str, Any], *refunds: tuple[int, str]) -> dict[str, Any]:
    """`state` with its expanded refunds replaced by these (amount, status) pairs, each shaped
    like the recorded first refund."""
    recorded = recorded_fetch(f"payment-captured-partly-refunded-{PARTLY_REFUNDED}")
    template = recorded["refunds"]["items"][0]
    items = [
        {**template, "id": f"rfnd_Test{n:010d}", "amount": amount, "status": status}
        for n, (amount, status) in enumerate(refunds)
    ]
    return {**state, "refunds": {"entity": "collection", "count": len(items), "items": items}}


async def test_processed_refunds_equal_to_the_amount_make_it_refunded(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    recorded = recorded_fetch(f"payment-captured-partly-refunded-{PARTLY_REFUNDED}")
    state = with_refunds(
        {**recorded, "status": "refunded"}, (30000, "processed"), (20000, "processed")
    )

    row = await ledger_row_after_fetch(writer, ledger, razorpay, read_rows, state)

    assert row == {"status": "refunded", "refunded_minor": 50000}


async def test_a_pending_refund_counts_for_nothing_until_it_is_processed(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    recorded = recorded_fetch(f"payment-captured-partly-refunded-{PARTLY_REFUNDED}")
    fetch = razorpay.get(f"/v1/payments/{PARTLY_REFUNDED}")
    fetch.respond(200, json=with_refunds(recorded, (10000, "pending")))
    await enqueue_ledger_job(writer, PARTLY_REFUNDED)
    await ledger.run_once()
    pending = await read_rows("SELECT status, refunded_minor, row_version FROM payments")

    fetch.respond(200, json=with_refunds(recorded, (10000, "processed")))
    await enqueue_ledger_job(writer, PARTLY_REFUNDED)
    await ledger.run_once()

    assert pending == [{"status": "succeeded", "refunded_minor": 0, "row_version": 1}]
    assert await read_rows("SELECT status, refunded_minor, row_version FROM payments") == [
        {"status": "partially_refunded", "refunded_minor": 10000, "row_version": 2}
    ]


async def test_a_failed_refund_alone_refunds_nothing(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    documented = recorded_fetch("unrecorded-refund-failed")  # its one refund failed

    row = await ledger_row_after_fetch(writer, ledger, razorpay, read_rows, documented)

    assert row == {"status": "succeeded", "refunded_minor": 0}


async def test_a_failed_refund_leaves_the_refunded_amount_at_the_processed_sum(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    recorded = recorded_fetch(f"payment-captured-partly-refunded-{PARTLY_REFUNDED}")
    state = with_refunds(recorded, (10000, "processed"), (25000, "failed"), (5000, "processed"))

    row = await ledger_row_after_fetch(writer, ledger, razorpay, read_rows, state)

    assert row == {"status": "partially_refunded", "refunded_minor": 15000}


async def test_an_uncaptured_authorisation_auto_refunded_is_canceled(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    documented = recorded_fetch("unrecorded-authorized-auto-refunded")

    row = await ledger_row_after_fetch(writer, ledger, razorpay, read_rows, documented)

    assert row == {"status": "canceled", "refunded_minor": 0}
