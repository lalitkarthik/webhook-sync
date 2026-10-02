"""Method, metadata, customer and international amounts in the ledger row.

Seam: the ledger stage on P1's stage runner, its jobs created by `enqueue`, Razorpay's API
answered by respx with the recorded bodies and Razorpay's documented samples
(`tests/recordings/razorpay/fetch/`). Method carries one non-personal detail (A8); metadata is
the configured `notes` keys (A10); the email and description are kept, never the phone, VPA or
card digits (A4); an international payment's fee and net are in its settlement currency (A9).
"""

import asyncio
import json
import logging
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
    TEST_WEBHOOK_SECRET,
    ReadRows,
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


def with_notes(notes: object) -> dict[str, Any]:
    """The recorded captured netbanking payment with these `notes`."""
    return {**recorded_fetch("payment-captured-pay_Tj9rKBOXWcsMeE"), "notes": notes}


@pytest.mark.parametrize(
    ("notes", "metadata"),
    [
        ({"order_id": "ord-1042", "other": "kept out"}, {"order_id": "ord-1042"}),
        ([], {}),  # Razorpay sends empty notes as a list
        ({"other": "kept out"}, {}),  # a configured key that is missing is left out
        ({"order_id": 1042}, {"order_id": 1042}),  # a value that isn't a string stays JSON
        ({"order_id": {"line": [1, 2]}}, {"order_id": {"line": [1, 2]}}),
    ],
)
async def test_metadata_is_the_configured_notes_keys(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    notes: object,
    metadata: dict[str, Any],
) -> None:
    await fetch_once(writer, ledger, razorpay, with_notes(notes))

    [row] = await read_rows("SELECT metadata FROM payments")
    assert json.loads(row["metadata"]) == metadata
    assert await read_rows("SELECT status FROM jobs") == [{"status": "done"}]


async def test_serve_uses_the_configured_metadata_keys(
    config: Config,
    data_dir: Path,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_module, "IDLE_WAIT", timedelta(milliseconds=10))
    configured = replace(
        config,
        razorpay=replace(
            config.razorpay,
            account=RECORDED_ACCOUNT,
            api_url=RAZORPAY_API,
            metadata_keys=METADATA_KEYS,
        ),
    )
    razorpay.get("/v1/payments/pay_Tj9rKBOXWcsMeE").respond(
        200, json=with_notes({"order_id": "ord-1042", "other": "kept out"})
    )
    body, headers = sign_razorpay(recorded_delivery("Tj9sMcl7hE9PaT"), event_id="Tj9sMcl7hE9PaT")

    app = create_app(configured, data_dir)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://wsync.test") as client:
            await client.post("/webhooks/razorpay", content=body, headers=headers)
            for _ in range(500):
                if await read_rows("SELECT metadata FROM payments"):
                    break
                await asyncio.sleep(0.01)

    [row] = await read_rows("SELECT metadata FROM payments")
    assert json.loads(row["metadata"]) == {"order_id": "ord-1042"}


PHONE = "+919876543210"
VPA = "gaurav.kumar@okhdfcbank"
LAST4 = "4242"
CARD_NAME = "Gaurav Kumar"


def with_personal_data(state: dict[str, Any]) -> dict[str, Any]:
    """`state` with the personal data the recordings had redacted put back: the phone, a VPA,
    the card's last4 and name, and a phone in an unconfigured `notes` key."""
    card = state.get("card")
    return {
        **state,
        "contact": PHONE,
        "vpa": VPA,
        "upi": {**(state.get("upi") or {}), "vpa": VPA},
        "card": None if card is None else {**card, "last4": LAST4, "name": CARD_NAME},
        "notes": {"order_id": "ord-1042", "phone": PHONE},
    }


@pytest.mark.parametrize(
    "name",
    [
        "unrecorded-card-captured-expanded",
        "unrecorded-card-failed-expanded",
        "unrecorded-upi-captured",
        "unrecorded-wallet-captured",
        "unrecorded-international-captured",
        "payment-captured-pay_Tj66x2JuEXjIdB",
    ],
)
async def test_no_row_or_log_line_holds_a_phone_vpa_or_card_digits(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    caplog: pytest.LogCaptureFixture,
    name: str,
) -> None:
    caplog.set_level(logging.DEBUG)

    await fetch_once(writer, ledger, razorpay, with_personal_data(recorded_fetch(name)))

    [row] = await read_rows("SELECT * FROM payments")
    logged = [json.dumps(vars(record), default=str) for record in caplog.records]
    assert logged  # the fetch was logged
    for stored in [json.dumps(row), *logged]:
        for personal in (PHONE, "9876543210", VPA, LAST4, CARD_NAME):
            assert personal not in stored


async def test_the_customer_email_and_description_are_kept_as_reported(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    state = recorded_fetch("payment-captured-pay_Tj66x2JuEXjIdB")

    await fetch_once(writer, ledger, razorpay, state)

    assert await read_rows("SELECT customer_email, customer_name, description FROM payments") == [
        {
            "customer_email": "customer@example.com",
            "customer_name": None,
            "description": "#Tj65RlqVNHm2LS",
        }
    ]


async def test_a_upi_payment_with_no_email_keeps_none(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    await fetch_once(writer, ledger, razorpay, recorded_fetch("unrecorded-upi-captured"))

    assert await read_rows("SELECT method, customer_email, description FROM payments") == [
        {"method": "upi", "customer_email": None, "description": "#MLXS5jI0oFNX2a"}
    ]
