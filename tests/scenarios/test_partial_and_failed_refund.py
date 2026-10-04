"""Scenario: a partial refund and a failed refund. Each leaves the right refund state on the
payment's own row (testing.md, "Scenarios"; user story 16; C1, C6, G15).

A captured payment of 500 is refunded twice, 100 each time, as recorded. Razorpay's refund
events come late and in no promised order: here each `refund.processed` arrives before its
`refund.created`, the first refund's `refund.created` after the second refund is processed, and
the second's last of all. A third refund then fails, from Razorpay's documented sample (Q166),
and its `refund.failed` arrives before its `refund.created`: its money never moved, so the row
never shows it. Each event only triggers a fetch (ADR 0002), so the row takes Razorpay's
current state whatever the order. Seam: the scenarios' harness.
"""

from typing import Any

import httpx
import pytest
from fakes.razorpay import recorded_refund
from scenarios.harness import Scenario
from support import ReadRows

from webhook_sync.sinks.sheets import DAILY_SUMMARY, PAYMENTS

PAYMENT = "pay_Tj9gmEfed6kRqO"  # INR 500 by netbanking, fee 11.80
CAPTURE = "Tj9h5SOW6XVf7g"  # its payment.captured
FIRST = "rfnd_Tj9tB2hm5WLvww"
FIRST_CREATED = "Tj9tkWQjOGc6cJ"
FIRST_PROCESSED = "Tj9tnpcnWxNGXY"
SECOND = "rfnd_Tj9tMhKlOulF3Q"
SECOND_CREATED = "Tj9tw9WT2xqs9D"  # arrives last of the four, after `refund_twice`
SECOND_PROCESSED = "Tj9tzY1GfR6cQF"
ROW = "SELECT payment_id, status, refunded_minor, row_version FROM payments"


def on_tab(scenario: Scenario, title: str) -> list[dict[str, Any]]:
    """Each row of the tab below its header, by column header."""
    header, *rows = scenario.sheets.tab(title).values()
    return [dict(zip(header, row, strict=False)) for row in rows]


async def refund_twice(
    scenario: Scenario, client: httpx.AsyncClient, read_rows: ReadRows
) -> list[list[dict[str, Any]]]:
    """The payment is captured and delivered, then refunded twice as recorded, the refunds'
    deliveries out of order and the second's `refund.created` still to come; the ledger's rows
    after the capture and after each refund."""
    razorpay = scenario.razorpay
    await razorpay.deliver(client, razorpay.delivery(CAPTURE))
    await scenario.settle()
    captured = await read_rows(ROW)

    razorpay.refund(recorded_refund(FIRST, "pending"))
    razorpay.refund(recorded_refund(FIRST))
    await razorpay.deliver(client, razorpay.delivery(FIRST_PROCESSED))
    await scenario.settle()
    once = await read_rows(ROW)

    razorpay.refund(recorded_refund(SECOND, "pending"))
    razorpay.refund(recorded_refund(SECOND))
    for event_id in (SECOND_PROCESSED, FIRST_CREATED):
        await razorpay.deliver(client, razorpay.delivery(event_id))
    await scenario.settle()
    return [captured, once, await read_rows(ROW)]


async def test_a_partial_refund_shows_on_the_payments_own_row_and_in_the_daily_summary(
    scenario: Scenario, read_rows: ReadRows
) -> None:
    razorpay = scenario.razorpay
    razorpay.before_refunds(PAYMENT)

    async with scenario.running() as client:
        rows = await refund_twice(scenario, client, read_rows)
        await razorpay.deliver(client, razorpay.delivery(SECOND_CREATED))
        await scenario.settle()

    assert await read_rows(ROW) == rows[-1]
    assert rows == [
        [{"payment_id": PAYMENT, "status": "succeeded", "refunded_minor": 0, "row_version": 1}],
        [
            {
                "payment_id": PAYMENT,
                "status": "partially_refunded",
                "refunded_minor": 10000,
                "row_version": 2,
            }
        ],
        [
            {
                "payment_id": PAYMENT,
                "status": "partially_refunded",
                "refunded_minor": 20000,
                "row_version": 3,
            }
        ],
    ]
    [on_sheet] = on_tab(scenario, PAYMENTS)
    assert (on_sheet["Payment ID"], on_sheet["Status"], on_sheet["Amount"]) == (
        PAYMENT,
        "partially_refunded",
        500,
    )
    assert on_sheet["Refunded"] == 200
    [day] = on_tab(scenario, DAILY_SUMMARY)
    assert (day["Payments"], day["Gross"], day["Refunds"], day["Fees"]) == (1, 500, 200, 11.8)
    assert day["Net (before refunds)"] == 488.2
    # All five deliveries are stored, each once: none was a duplicate of another.
    assert await read_rows(
        "SELECT type, count(*) AS n FROM events WHERE origin = 'delivery' GROUP BY type"
        " ORDER BY type"
    ) == [
        {"type": "payment.captured", "n": 1},
        {"type": "refund.created", "n": 2},
        {"type": "refund.processed", "n": 2},
    ]
    await scenario.check()


@pytest.mark.unrecorded
async def test_a_refund_that_fails_leaves_the_row_at_the_money_that_moved(
    scenario: Scenario, read_rows: ReadRows
) -> None:
    razorpay = scenario.razorpay
    razorpay.before_refunds(PAYMENT)

    async with scenario.running() as client:
        *_, refunded = await refund_twice(scenario, client, read_rows)
        failing = razorpay.documented_refund(PAYMENT, amount=10000)
        razorpay.refund(failing | {"status": "pending"})
        # The second refund's late delivery makes us fetch while the third is pending.
        await razorpay.deliver(client, razorpay.delivery(SECOND_CREATED))
        await scenario.settle()
        pending = await read_rows(ROW)

        razorpay.refund(failing)
        for event_type in ("refund.failed", "refund.created"):
            delivery = razorpay.documented_delivery(event_type, failing["id"])
            await razorpay.deliver(client, delivery)
        await scenario.settle()

    # Razorpay's own total counted the refund while it was pending; the row never did.
    counted = [state["amount_refunded"] for state in razorpay.states(PAYMENT)[-3:]]
    assert counted == [20000, 30000, 20000]
    assert pending == refunded
    assert await read_rows(ROW) == refunded
    assert refunded == [
        {
            "payment_id": PAYMENT,
            "status": "partially_refunded",
            "refunded_minor": 20000,
            "row_version": 3,
        }
    ]
    [on_sheet] = on_tab(scenario, PAYMENTS)
    assert (on_sheet["Status"], on_sheet["Refunded"]) == ("partially_refunded", 200)
    [day] = on_tab(scenario, DAILY_SUMMARY)
    assert (day["Payments"], day["Gross"], day["Refunds"]) == (1, 500, 200)
    assert await read_rows(
        "SELECT type FROM events WHERE event_id LIKE 'unrecorded-%' ORDER BY type"
    ) == [{"type": "refund.created"}, {"type": "refund.failed"}]
    await scenario.check()
