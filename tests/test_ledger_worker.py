"""The ledger worker: a ledger job becomes the payment's business row (seam: the ledger stage).

The ledger stage runs on P1's stage runner, its jobs created by a signed delivery through the
HTTP app or by `enqueue`, Razorpay's API answered by respx with the recorded test-mode bodies
(`tests/recordings/razorpay/fetch/`). SQLite is real; time is frozen with time-machine.
"""

import asyncio
import base64
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from support import (
    RECORDED_ACCOUNT,
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    ReadRows,
    TEST_WEBHOOK_SECRET,
    recorded_api_answer,
    recorded_delivery,
    recorded_fetch,
    sign_razorpay,
)
from time_machine import TimeMachineFixture

from webhook_sync.clock import utc_now
from webhook_sync.config import Config
from webhook_sync.db.writer import Writer
from webhook_sync.ledger import ledger_runner
from webhook_sync.queue import Pauses, StageRunner, enqueue
from webhook_sync.queue import runner as runner_module
from webhook_sync.receiver.app import create_app
from webhook_sync.sources.razorpay import RazorpayAdapter, api_client

RAZORPAY_API = "https://api.razorpay.test"
NINE = datetime(2026, 10, 2, 20, 30, tzinfo=UTC)


@pytest.fixture(autouse=True)
def frozen_clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(NINE, tick=False)
    return time_machine


@pytest.fixture
def recorded_config(config: Config) -> Config:
    """The test config as the recordings need it: their account, and the faked API's address."""
    return replace(
        config, razorpay=replace(config.razorpay, account=RECORDED_ACCOUNT, api_url=RAZORPAY_API)
    )


@pytest.fixture
def razorpay() -> Iterator[respx.MockRouter]:
    """Razorpay's API, answered by respx; a request no route matches fails the test."""
    with respx.mock(base_url=RAZORPAY_API, assert_all_called=False) as router:
        yield router


def answer(name: str) -> httpx.Response:
    return httpx.Response(200, json=recorded_fetch(name))


def fetched_request(route: respx.Route) -> httpx.Request:
    """The request a route last answered."""
    return route.calls.last.request


async def enqueue_ledger_job(
    writer: Writer, payment_key: str, event_id: str | None = None
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


async def wait_for(read_rows: ReadRows, sql: str, expected: list[dict[str, Any]]) -> None:
    """Wait (in real time) until `sql` reads `expected`: the app's runner works in the
    background."""
    for _ in range(500):
        if await read_rows(sql) == expected:
            return
        await asyncio.sleep(0.01)
    assert await read_rows(sql) == expected


async def test_a_delivered_captured_payment_becomes_its_ledger_row(
    recorded_config: Config,
    data_dir: Path,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "IDLE_WAIT", timedelta(milliseconds=10))
    fetch = razorpay.get("/v1/payments/pay_Tj9rKBOXWcsMeE").mock(
        return_value=answer("payment-captured-pay_Tj9rKBOXWcsMeE")
    )
    body, headers = sign_razorpay(recorded_delivery("Tj9sMcl7hE9PaT"), event_id="Tj9sMcl7hE9PaT")

    app = create_app(recorded_config, data_dir)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://wsync.test") as client:
            response = await client.post("/webhooks/razorpay", content=body, headers=headers)
            await wait_for(read_rows, "SELECT status FROM jobs", [{"status": "done"}])

    assert response.status_code == 202
    [row] = await read_rows("SELECT * FROM payments")
    assert {k: row[k] for k in ("source", "account", "payment_id")} == {
        "source": "razorpay",
        "account": RECORDED_ACCOUNT,
        "payment_id": "pay_Tj9rKBOXWcsMeE",
    }
    assert {
        k: row[k]
        for k in (
            "status",
            "source_status",
            "amount_minor",
            "currency",
            "fee_minor",
            "fee_tax_minor",
            "net_minor",
            "settlement_currency",
            "source_created_at",
            "last_event_id",
            "via",
            "row_version",
        )
    } == {
        "status": "succeeded",
        "source_status": "captured",
        "amount_minor": 50000,
        "currency": "INR",
        "fee_minor": 1180,
        "fee_tax_minor": 180,
        "net_minor": 48820,
        "settlement_currency": "INR",
        "source_created_at": "2026-10-02T19:38:58.000000Z",  # created_at 1790969938
        "last_event_id": "Tj9sMcl7hE9PaT",
        "via": "delivery",
        "row_version": 1,
    }
    assert row["updated_at"] == "2026-10-02T20:30:00.000000Z"
    assert fetch.call_count == 1
    request = fetched_request(fetch)
    assert request.url.params.multi_items() == [("expand[]", "refunds"), ("expand[]", "card")]
    credentials = base64.b64encode(f"{TEST_KEY_ID}:{TEST_KEY_SECRET}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {credentials}"


async def test_an_authorized_payment_has_its_fee_and_net_pending(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    authorized = recorded_api_answer("20261003T010947-pay_Tj9rKBOXWcsMeE-authorized")
    razorpay.get("/v1/payments/pay_Tj9rKBOXWcsMeE").respond(200, json=authorized)
    await enqueue_ledger_job(writer, "pay_Tj9rKBOXWcsMeE")

    await ledger.run_once()

    assert await read_rows(
        "SELECT status, source_status, amount_minor, fee_minor, fee_tax_minor, net_minor"
        " FROM payments"
    ) == [
        {
            "status": "authorized",
            "source_status": "authorized",
            "amount_minor": 50000,
            "fee_minor": None,
            "fee_tax_minor": None,
            "net_minor": None,
        }
    ]
    assert await read_rows("SELECT status FROM jobs") == [{"status": "done"}]


async def test_a_failed_payment_gives_a_failed_row(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    razorpay.get("/v1/payments/pay_Tj9sm13R8h2378").mock(
        return_value=answer("payment-failed-pay_Tj9sm13R8h2378")
    )
    await enqueue_ledger_job(writer, "pay_Tj9sm13R8h2378")

    await ledger.run_once()

    assert await read_rows(
        "SELECT payment_id, status, source_status, fee_minor, net_minor FROM payments"
    ) == [
        {
            "payment_id": "pay_Tj9sm13R8h2378",
            "status": "failed",
            "source_status": "failed",
            "fee_minor": None,
            "net_minor": None,
        }
    ]
    assert await read_rows("SELECT status FROM jobs") == [{"status": "done"}]


async def test_a_payment_still_created_gives_no_row_and_a_done_job(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    razorpay.get("/v1/payments/pay_Tj64GJlPGe5X07").mock(
        return_value=answer("payment-created-pay_Tj64GJlPGe5X07")
    )
    await enqueue_ledger_job(writer, "pay_Tj64GJlPGe5X07")

    await ledger.run_once()

    assert await read_rows("SELECT * FROM payments") == []
    assert await read_rows("SELECT status FROM jobs") == [{"status": "done"}]


async def test_two_fetches_of_the_same_state_leave_one_row_at_version_1(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    frozen_clock: TimeMachineFixture,
) -> None:
    fetch = razorpay.get("/v1/payments/pay_Tj9rKBOXWcsMeE").mock(
        return_value=answer("payment-captured-pay_Tj9rKBOXWcsMeE")
    )
    await enqueue_ledger_job(writer, "pay_Tj9rKBOXWcsMeE")
    await ledger.run_once()
    frozen_clock.shift(timedelta(minutes=5))
    await enqueue_ledger_job(writer, "pay_Tj9rKBOXWcsMeE")

    await ledger.run_once()

    assert fetch.call_count == 2
    assert await read_rows("SELECT status, row_version, updated_at FROM payments") == [
        {"status": "succeeded", "row_version": 1, "updated_at": "2026-10-02T20:30:00.000000Z"}
    ]
    assert await read_rows("SELECT status FROM jobs") == [{"status": "done"}, {"status": "done"}]
