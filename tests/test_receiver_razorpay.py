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
