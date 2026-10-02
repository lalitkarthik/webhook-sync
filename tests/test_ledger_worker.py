"""The ledger worker: a ledger job becomes the payment's business row (seam: the ledger stage).

The ledger stage runs on P1's stage runner, its jobs created by a signed delivery through the
HTTP app or by `enqueue`, Razorpay's API answered by respx with the recorded test-mode bodies
(`tests/recordings/razorpay/fetch/`). SQLite is real; time is frozen with time-machine.
"""

import asyncio
import base64
from collections.abc import Iterator
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
    recorded_delivery,
    recorded_fetch,
    sign_razorpay,
)
from time_machine import TimeMachineFixture

from webhook_sync.config import Config
from webhook_sync.queue import runner as runner_module
from webhook_sync.receiver.app import create_app

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
