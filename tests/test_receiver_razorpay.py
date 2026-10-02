"""A signed Razorpay delivery is stored as exactly one event (seam 1: the HTTP app in process)."""

import hashlib
from datetime import UTC, datetime

import httpx
import pytest
from support import TEST_ACCOUNT, ReadRows, razorpay_sample, sign_razorpay

pytestmark = pytest.mark.unrecorded


def fingerprint(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


async def test_a_signed_payment_captured_is_stored_as_one_new_event(
    client: httpx.AsyncClient, read_rows: ReadRows
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_captured01")

    response = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 202
    events = await read_rows("SELECT * FROM events")
    assert len(events) == 1
    event = events[0]
    assert event["source"] == "razorpay"
    assert event["event_id"] == "evt_captured01"
    assert event["type"] == "payment.captured"
    assert event["account"] == TEST_ACCOUNT
    assert event["mode"] == "test"
    assert event["payment_key"] == "pay_DESlfW9H8K9uqM"
    assert event["origin"] == "delivery"
    # Q117: type | primary entity id | top-level created_at | mode
    assert event["fingerprint"] == fingerprint(
        "payment.captured", "pay_DESlfW9H8K9uqM", "1567674606", "test"
    )
    assert event["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert datetime.fromisoformat(event["source_created_at"]) == datetime.fromtimestamp(
        1567674606, UTC
    )
    deliveries = await read_rows("SELECT * FROM deliveries")
    assert [(d["event_id"], d["result"], d["status_code"]) for d in deliveries] == [
        ("evt_captured01", "new", 202)
    ]


async def test_the_same_delivery_again_is_a_counted_duplicate(
    client: httpx.AsyncClient, read_rows: ReadRows
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_captured01")
    first = await client.post("/webhooks/razorpay", content=body, headers=headers)

    again = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert (first.status_code, again.status_code) == (202, 200)
    assert len(await read_rows("SELECT * FROM events")) == 1
    deliveries = await read_rows("SELECT * FROM deliveries ORDER BY id")
    assert [(d["event_id"], d["result"], d["status_code"]) for d in deliveries] == [
        ("evt_captured01", "new", 202),
        ("evt_captured01", "duplicate", 200),
    ]


@pytest.mark.parametrize(
    ("event_type", "payment_key"),
    [
        ("payment.dispute.created", "pay_EFtmUsbwpXwBHI"),
        ("order.paid", "pay_DESlfW9H8K9uqM"),
    ],
)
async def test_an_unhandled_type_is_stored_as_ignored_with_a_reason_and_keeps_its_payment(
    client: httpx.AsyncClient, read_rows: ReadRows, event_type: str, payment_key: str
) -> None:
    body, headers = sign_razorpay(razorpay_sample(event_type), event_id="evt_ignored01")

    response = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 202
    events = await read_rows("SELECT * FROM events")
    assert [(e["type"], e["outcome"], e["ignore_reason"], e["payment_key"]) for e in events] == [
        (event_type, "ignored", "unhandled_type", payment_key)
    ]
    deliveries = await read_rows("SELECT result, status_code FROM deliveries")
    assert deliveries == [{"result": "new", "status_code": 202}]


async def test_a_dispute_is_fingerprinted_by_the_dispute_not_the_payment(
    client: httpx.AsyncClient, read_rows: ReadRows
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.dispute.created"))

    await client.post("/webhooks/razorpay", content=body, headers=headers)

    [event] = await read_rows("SELECT fingerprint FROM events")
    assert event["fingerprint"] == fingerprint(
        "payment.dispute.created", "disp_EsIAlDcoUr8CaQ", "1589907977", "test"
    )
