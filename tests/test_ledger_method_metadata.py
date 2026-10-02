"""Method, metadata, customer and international amounts in the ledger row.

Seam: the ledger stage on P1's stage runner, its jobs created by `enqueue`, Razorpay's API
answered by respx with the recorded bodies and Razorpay's documented samples
(`tests/recordings/razorpay/fetch/`). Method carries one non-personal detail (A8); metadata is
the configured `notes` keys (A10); the email and description are kept, never the phone, VPA or
card digits (A4); an international payment's fee and net are in its settlement currency (A9).
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
METADATA_KEYS = ("order_id",)


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
    """The ledger stage on the stage runner, with `order_id` as the configured metadata key."""
    async with api_client(RAZORPAY_API, TEST_KEY_ID, TEST_KEY_SECRET) as api:
        adapter = RazorpayAdapter(
            mode="test",
            account=RECORDED_ACCOUNT,
            webhook_secrets=(TEST_WEBHOOK_SECRET,),
            api=api,
            metadata_keys=METADATA_KEYS,
        )
        yield ledger_runner(
            writer,
            {"razorpay": adapter},
            claimed_by="wsync-test",
            pauses=Pauses(),
            retry_profile="production",
            sleep=never_wakes,
        )


async def fetch_once(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, state: dict[str, Any]
) -> None:
    """Answer the payment's fetch with `state` and run one ledger job for it."""
    razorpay.get(f"/v1/payments/{state['id']}").respond(200, json=state)
    async with writer.transaction() as tx:
        await enqueue(
            tx,
            stage="ledger",
            sink="ledger",
            source="razorpay",
            account=RECORDED_ACCOUNT,
            payment_key=state["id"],
            event_id=None,
            retry_profile="production",
            now=utc_now(),
        )
    await ledger.run_once()


@pytest.mark.parametrize(
    ("name", "method"),
    [
        ("unrecorded-card-captured-expanded", "card (Visa)"),
        ("unrecorded-card-failed-expanded", "card (RuPay)"),
        ("payment-captured-pay_Tj9rKBOXWcsMeE", "netbanking (CNRB)"),
        ("unrecorded-wallet-captured", "wallet (airtelmoney)"),
        ("unrecorded-upi-captured", "upi"),
    ],
)
async def test_the_method_shows_one_non_personal_detail(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    name: str,
    method: str,
) -> None:
    await fetch_once(writer, ledger, razorpay, recorded_fetch(name))

    assert await read_rows("SELECT method FROM payments") == [{"method": method}]
