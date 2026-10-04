"""FakeRazorpay's deliveries: a recorded delivery, re-signed with the fixture secret, sent to the
receiver once, many times, under another event id, or with another body under the same id.

Seam: the scenarios' harness (the app in process, respx, the controlled clock).
"""

import hashlib
import hmac
import json

from scenarios.harness import Scenario
from support import TEST_WEBHOOK_SECRET, ReadRows, recorded_delivery

CAPTURE = "Tj9sMcl7hE9PaT"  # payment.captured of pay_Tj9rKBOXWcsMeE, recorded byte-exact
CAPTURED = "pay_Tj9rKBOXWcsMeE"


def test_a_delivery_replays_the_recorded_body_and_headers_signed_with_the_fixture_secret(
    scenario: Scenario,
) -> None:
    delivery = scenario.razorpay.delivery(CAPTURE)

    assert delivery.body == recorded_delivery(CAPTURE)
    assert delivery.event_id == CAPTURE
    assert delivery.payment_id == CAPTURED
    assert delivery.headers["user-agent"] == "Razorpay-Webhook/v1"
    assert delivery.headers["request-id"] == CAPTURE
    signature = hmac.new(TEST_WEBHOOK_SECRET.encode(), delivery.body, hashlib.sha256).hexdigest()
    assert delivery.headers["x-razorpay-signature"] == signature


def test_under_another_event_id_the_bytes_and_signature_stay(scenario: Scenario) -> None:
    delivery = scenario.razorpay.delivery(CAPTURE)

    other = delivery.under("Tj9sMcl7hE9Pzz")

    assert (other.body, other.signature) == (delivery.body, delivery.signature)
    assert other.event_id == "Tj9sMcl7hE9Pzz"


def test_with_another_body_the_event_id_stays_and_the_body_is_re_signed(
    scenario: Scenario,
) -> None:
    delivery = scenario.razorpay.delivery(CAPTURE)
    body = json.loads(delivery.body)
    body["created_at"] += 60

    other = delivery.with_body(body)

    assert other.event_id == CAPTURE
    assert json.loads(other.body)["created_at"] == body["created_at"]
    signature = hmac.new(TEST_WEBHOOK_SECRET.encode(), other.body, hashlib.sha256).hexdigest()
    assert other.signature == signature != delivery.signature


async def test_a_delivery_sent_three_times_is_answered_three_times_and_its_payment_is_known(
    scenario: Scenario, read_rows: ReadRows
) -> None:
    delivery = scenario.razorpay.delivery(CAPTURE)

    async with scenario.running() as client:
        sent = await scenario.razorpay.deliver(client, delivery, times=3)

    assert sorted(s.status_code for s in sent) == [200, 200, 202]
    assert scenario.razorpay.sent == sent
    assert CAPTURED in scenario.razorpay.payments
    assert await read_rows("SELECT result FROM deliveries ORDER BY result") == [
        {"result": "duplicate"},
        {"result": "duplicate"},
        {"result": "new"},
    ]
