"""FakeRazorpay's refunds: a recorded payment taken back to before its refunds, each recorded
refund replayed (created `pending`, then `processed`), and a refund that fails, from Razorpay's
documented sample (testing.md, "FakeRazorpay"; Q166).

Seam: FakeRazorpay through respx, as the ledger worker and the sweeper call it. The recorded
refunds are held to the recordings: replaying them gives the recorded answers back.
"""

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest
import respx
from fakes.razorpay import (
    RAZORPAY_API,
    FakeRazorpay,
    recorded_fetches,
    recorded_listed_payments,
    recorded_listed_refunds,
    recorded_refund,
)
from scenarios.harness import NOW
from support import (
    RECORDED_ACCOUNT,
    TEST_WEBHOOK_SECRET,
    razorpay_sample,
    recorded_api_answer,
    recorded_fetch,
)

from webhook_sync.sources.razorpay import payment_row

PAYMENT = "pay_Tj9gmEfed6kRqO"  # captured, then refunded twice, 100 of 500 each time
FIRST = "rfnd_Tj9tB2hm5WLvww"
SECOND = "rfnd_Tj9tMhKlOulF3Q"
NO_REFUNDS: dict[str, Any] = {"entity": "collection", "count": 0, "items": []}


@pytest.fixture
def razorpay() -> FakeRazorpay:
    fake = FakeRazorpay(now=NOW)
    fake.before_refunds(PAYMENT)
    return fake


async def answered(razorpay: FakeRazorpay, path: str, **params: int) -> dict[str, Any]:
    with respx.mock(assert_all_called=False) as router:
        razorpay.mount(router)
        async with httpx.AsyncClient(base_url=RAZORPAY_API) as client:
            return (await client.get(path, params=params)).json()


def refunded(razorpay: FakeRazorpay) -> tuple[str, int]:
    """Our status and refunded amount for the payment's current state at Razorpay."""
    row = payment_row(razorpay.fetches[PAYMENT])
    assert row is not None
    return row.status, row.refunded_minor


def test_a_recorded_refund_is_pending_as_its_creation_answered_and_processed_as_its_fetch_did() -> (
    None
):
    created = recorded_api_answer("20261003T011044-rfnd_Tj9tB2hm5WLvww-pending")
    fetched = recorded_api_answer("20261003T011109-rfnd_Tj9tB2hm5WLvww-processed")
    del created["base_amount"]  # only the answer to the creation carries it
    fetched["acquirer_data"] = {"arn": "<redacted>"}  # as the list and the payment's fetch show it

    assert recorded_refund(FIRST, "pending") == created
    assert recorded_refund(FIRST) == fetched


async def test_before_its_refunds_a_recorded_payment_is_captured_with_nothing_refunded(
    razorpay: FakeRazorpay,
) -> None:
    none = {"amount_refunded": 0, "refund_status": None}

    assert razorpay.fetches[PAYMENT] == recorded_fetches()[PAYMENT] | none | {"refunds": NO_REFUNDS}
    assert razorpay.payments == {PAYMENT: recorded_listed_payments()[PAYMENT] | none}
    assert razorpay.refunds == []
    assert refunded(razorpay) == ("succeeded", 0)


async def test_replaying_both_recorded_refunds_gives_the_recorded_fetch_and_lists(
    razorpay: FakeRazorpay,
) -> None:
    for refund_id in (FIRST, SECOND):
        razorpay.refund(recorded_refund(refund_id, "pending"))
        razorpay.refund(recorded_refund(refund_id))

    assert await answered(razorpay, f"/v1/payments/{PAYMENT}") == recorded_fetches()[PAYMENT]
    in_2026 = {"from": 1790000000, "to": 1800000000, "count": 100}
    assert (await answered(razorpay, "/v1/refunds", **in_2026))["items"] == (
        recorded_listed_refunds()
    )
    assert (await answered(razorpay, "/v1/payments", **in_2026))["items"] == [
        recorded_listed_payments()[PAYMENT]
    ]
    page = await answered(razorpay, f"/v1/payments/{PAYMENT}/refunds", count=1, skip=1)
    assert page == recorded_fetch("refunds-page-2")


def test_each_step_of_a_refund_is_a_state_the_payment_went_through(
    razorpay: FakeRazorpay,
) -> None:
    razorpay.refund(recorded_refund(FIRST, "pending"))
    pending = refunded(razorpay)
    razorpay.refund(recorded_refund(FIRST))

    assert pending == ("succeeded", 0)
    assert refunded(razorpay) == ("partially_refunded", 10000)
    statuses = [
        [refund["status"] for refund in state["refunds"]["items"]]
        for state in razorpay.states(PAYMENT)
    ]
    assert statuses == [[], ["pending"], ["processed"]]


@pytest.mark.unrecorded
def test_a_documented_refund_is_the_samples_failed_refund_placed_on_the_payment(
    razorpay: FakeRazorpay,
) -> None:
    sample = razorpay_sample("refund.failed")["payload"]["refund"]["entity"]

    failed = razorpay.documented_refund(PAYMENT, amount=10000)

    placed = {
        "payment_id": PAYMENT,
        "amount": 10000,
        "currency": "INR",
        "created_at": int(NOW.timestamp()),
    }
    assert failed == sample | placed
    assert failed["status"] == "failed"


@pytest.mark.unrecorded
def test_a_refund_that_fails_counts_at_razorpay_while_pending_and_never_in_our_row(
    razorpay: FakeRazorpay,
) -> None:
    razorpay.refund(recorded_refund(FIRST))
    failed = razorpay.documented_refund(PAYMENT, amount=10000)

    razorpay.refund(failed | {"status": "pending"})
    pending = razorpay.fetches[PAYMENT]
    while_pending = refunded(razorpay)
    razorpay.refund(failed)

    # Whether Razorpay's own total counts a pending refund is undocumented (G65): the fake's
    # does, so a row that followed `amount_refunded` would show money that never moved.
    assert (pending["amount_refunded"], pending["refund_status"]) == (20000, "partial")
    assert while_pending == ("partially_refunded", 10000)
    now = razorpay.fetches[PAYMENT]
    assert (now["amount_refunded"], now["refund_status"]) == (10000, "partial")
    assert [refund["status"] for refund in now["refunds"]["items"]] == ["failed", "processed"]
    assert razorpay.payments[PAYMENT]["amount_refunded"] == 10000
    assert [refund["status"] for refund in razorpay.refunds] == ["processed", "failed"]
    assert refunded(razorpay) == ("partially_refunded", 10000)


def totals(payment: dict[str, Any]) -> tuple[str, int, str | None]:
    return payment["status"], payment["amount_refunded"], payment["refund_status"]


@pytest.mark.unrecorded
def test_refunds_adding_up_to_the_amount_make_the_payment_refunded_in_full(
    razorpay: FakeRazorpay,
) -> None:
    for refund_id in (FIRST, SECOND):
        razorpay.refund(recorded_refund(refund_id))
    sample = razorpay_sample("refund.processed")["payload"]["refund"]["entity"]

    rest = razorpay.documented_refund(PAYMENT, amount=30000, sample="refund.processed")
    razorpay.refund(rest)

    assert (rest["id"], rest["status"]) == (sample["id"], "processed")
    assert totals(razorpay.fetches[PAYMENT]) == ("refunded", 50000, "full")
    assert razorpay.fetches[PAYMENT]["captured"] is True
    assert totals(razorpay.payments[PAYMENT]) == ("refunded", 50000, "full")
    assert refunded(razorpay) == ("refunded", 50000)


@pytest.mark.unrecorded
def test_a_refund_of_the_rest_that_fails_takes_the_payment_back_to_captured(
    razorpay: FakeRazorpay,
) -> None:
    for refund_id in (FIRST, SECOND):
        razorpay.refund(recorded_refund(refund_id))
    rest = razorpay.documented_refund(PAYMENT, amount=30000)

    razorpay.refund(rest | {"status": "pending"})
    pending = totals(razorpay.fetches[PAYMENT])
    while_pending = refunded(razorpay)
    razorpay.refund(rest)

    assert pending == ("refunded", 50000, "full")
    assert while_pending == ("partially_refunded", 20000)
    assert totals(razorpay.fetches[PAYMENT]) == ("captured", 20000, "partial")
    assert totals(razorpay.payments[PAYMENT]) == ("captured", 20000, "partial")
    assert refunded(razorpay) == ("partially_refunded", 20000)


@pytest.mark.unrecorded
def test_a_documented_delivery_is_the_sample_about_the_refund_signed_with_the_fixture_secret(
    razorpay: FakeRazorpay,
) -> None:
    failed = razorpay.documented_refund(PAYMENT, amount=10000)
    razorpay.refund(failed)
    sample = razorpay_sample("refund.failed")

    delivery = razorpay.documented_delivery("refund.failed", failed["id"])

    body = json.loads(delivery.body)
    payment = {k: v for k, v in razorpay.fetches[PAYMENT].items() if k != "refunds"}
    assert body == sample | {
        "account_id": RECORDED_ACCOUNT,
        "created_at": int(NOW.timestamp()),
        "payload": {"refund": {"entity": failed}, "payment": {"entity": payment}},
    }
    assert delivery.payment_id == PAYMENT
    assert delivery.event_id == f"unrecorded-refund.failed-{failed['id']}"
    assert delivery.headers["user-agent"] == "Razorpay-Webhook/v1"
    assert delivery.headers["request-id"] == delivery.event_id
    signature = hmac.new(TEST_WEBHOOK_SECRET.encode(), delivery.body, hashlib.sha256).hexdigest()
    assert delivery.signature == signature
    created = razorpay.documented_delivery("refund.created", failed["id"])
    assert json.loads(created.body)["event"] == "refund.created"
    assert created.event_id != delivery.event_id
