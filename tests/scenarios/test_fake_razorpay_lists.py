"""FakeRazorpay's lists and payments: a delivery that never arrives, a payment made at a given
second, a payment the list shows late, items created in the same second in a shifting order, a
payment appearing between two page calls, and a late authorisation (testing.md, "FakeRazorpay").

Seam: FakeRazorpay through respx, as the sweeper calls it.
"""

from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from fakes.razorpay import RAZORPAY_API, FakeRazorpay, recorded_listed_payments
from scenarios.harness import NOW

CAPTURE = "Tj9sMcl7hE9PaT"  # payment.captured of pay_Tj9rKBOXWcsMeE
FAILED = "pay_Tj9sm13R8h2378"  # failed at the bank, recorded
SECOND = int(NOW.timestamp())


@pytest.fixture
def razorpay() -> FakeRazorpay:
    return FakeRazorpay(now=NOW)


async def listed(
    razorpay: FakeRazorpay, start: int, end: int, *, count: int = 100, skip: int = 0
) -> list[dict[str, Any]]:
    with respx.mock(assert_all_called=False) as router:
        razorpay.mount(router)
        async with httpx.AsyncClient(base_url=RAZORPAY_API) as client:
            params = {"from": start, "to": end, "count": count, "skip": skip}
            answer = await client.get("/v1/payments", params=params)
    return answer.json()["items"]


async def fetched(razorpay: FakeRazorpay, payment_id: str) -> dict[str, Any]:
    with respx.mock(assert_all_called=False) as router:
        razorpay.mount(router)
        async with httpx.AsyncClient(base_url=RAZORPAY_API) as client:
            return (await client.get(f"/v1/payments/{payment_id}")).json()


def test_a_delivery_that_never_arrives_makes_its_payment_known_and_sends_nothing(
    razorpay: FakeRazorpay,
) -> None:
    delivery = razorpay.delivery(CAPTURE)

    razorpay.never(delivery)

    assert razorpay.payments == {
        "pay_Tj9rKBOXWcsMeE": recorded_listed_payments()["pay_Tj9rKBOXWcsMeE"]
    }
    assert (razorpay.sent, razorpay.unanswered) == ([], [])


async def test_a_made_payment_is_listed_and_fetched_under_its_own_id_and_second(
    razorpay: FakeRazorpay,
) -> None:
    razorpay.make("pay_Made0000000001", SECOND + 5)

    [item] = await listed(razorpay, SECOND, SECOND + 10)
    payment = await fetched(razorpay, "pay_Made0000000001")

    assert (item["id"], item["created_at"], item["status"]) == (
        "pay_Made0000000001",
        SECOND + 5,
        "captured",
    )
    assert (payment["id"], payment["created_at"], payment["fee"]) == (
        "pay_Made0000000001",
        SECOND + 5,
        1180,
    )


async def test_a_payment_listed_late_is_fetched_at_once_and_listed_only_once_it_is_that_old(
    razorpay: FakeRazorpay,
) -> None:
    razorpay.listed_late = timedelta(minutes=40)  # as measured (razorpay-list-facts.md, Q7)
    razorpay.make("pay_ListedLate0001", SECOND)

    at_once = await listed(razorpay, SECOND - 60, SECOND + 3600)
    payment = await fetched(razorpay, "pay_ListedLate0001")
    razorpay.now += timedelta(minutes=39, seconds=59)
    a_second_short = await listed(razorpay, SECOND - 60, SECOND + 3600)
    razorpay.now += timedelta(seconds=1)
    [item] = await listed(razorpay, SECOND - 60, SECOND + 3600)

    assert (at_once, a_second_short) == ([], [])
    assert (payment["id"], payment["status"]) == ("pay_ListedLate0001", "captured")
    assert (item["id"], item["created_at"]) == ("pay_ListedLate0001", SECOND)


async def test_items_created_in_the_same_second_come_back_in_a_shifting_order(
    razorpay: FakeRazorpay,
) -> None:
    razorpay.shifting = True
    for n in range(8):
        razorpay.make(f"pay_Tie0000000000{n}", SECOND)
    razorpay.make("pay_Older000000001", SECOND - 1)

    orders = [[item["id"] for item in await listed(razorpay, SECOND - 1, SECOND)] for _ in range(4)]

    assert len({tuple(order) for order in orders}) > 1
    assert {order[-1] for order in orders} == {"pay_Older000000001"}  # still newest first
    assert all(sorted(order) == sorted(orders[0]) for order in orders)


async def test_a_payment_arriving_between_page_calls_is_listed_from_the_next_call_on(
    razorpay: FakeRazorpay,
) -> None:
    for n in range(4):
        razorpay.make(f"pay_Paged00000000{n}", SECOND + n)
    razorpay.arriving.append(razorpay.made("pay_Arrived0000001", SECOND + 1))

    first = await listed(razorpay, SECOND, SECOND + 10, count=2)
    second = await listed(razorpay, SECOND, SECOND + 10, count=2, skip=2)

    assert "pay_Arrived0000001" not in {item["id"] for item in first}
    assert "pay_Arrived0000001" in {item["id"] for item in second}
    assert "pay_Arrived0000001" in razorpay.fetches
    assert razorpay.arriving == []


async def test_a_late_authorisation_moves_a_failed_payment_to_captured_with_no_delivery(
    razorpay: FakeRazorpay,
) -> None:
    razorpay.never(razorpay.delivery("Tj9sobwBnTgcRB"))  # payment.failed of FAILED

    razorpay.authorise_late(FAILED)

    assert [state["status"] for state in razorpay.states(FAILED)] == [
        "failed",
        "authorized",
        "captured",
    ]
    [item] = await listed(razorpay, SECOND - 3600, SECOND)
    payment = await fetched(razorpay, FAILED)
    assert (item["status"], item["captured"], item["error_code"]) == ("captured", True, None)
    assert (payment["status"], payment["fee"], payment["error_code"]) == ("captured", 1180, None)
    assert razorpay.sent == []
