"""Scenario: a partial refund and a failed refund. Each leaves the right refund state on the
payment's own row (testing.md, "Scenarios"; user story 16; C1, C5, C6, G15).

A captured payment of 500 is refunded twice, 100 each time, as recorded. Razorpay's events come
late and in no promised order. Here a refund's `refund.processed` is the first delivery we ever
see of the payment (C1, F7); its `payment.captured` arrives after that and its
`payment.authorized` after both refunds, each carrying a state older than the row; and each
`refund.created` arrives after its `refund.processed`. A third refund then fails, from
Razorpay's documented sample (Q166): its money never moved, so the row never shows it. Or it
is processed for all that was left, and the row is `refunded` (C5). Each event only triggers a
fetch (ADR 0002), so the row takes Razorpay's current state whatever the order. Seam: the
scenarios' harness.
"""

import json
from typing import Any, cast

import httpx
import pytest
import respx
from fakes.razorpay import recorded_refund
from scenarios.harness import Scenario
from support import ReadRows

from webhook_sync.sinks.sheets import DAILY_SUMMARY, PAYMENTS

PAYMENT = "pay_Tj9gmEfed6kRqO"  # INR 500 by netbanking, fee 11.80
AUTHORISATION = "Tj9h3likXEQFy4"  # its payment.authorized: not captured yet, no fee
CAPTURE = "Tj9h5SOW6XVf7g"  # its payment.captured: nothing refunded yet
FIRST = "rfnd_Tj9tB2hm5WLvww"
FIRST_CREATED = "Tj9tkWQjOGc6cJ"
FIRST_PROCESSED = "Tj9tnpcnWxNGXY"
SECOND = "rfnd_Tj9tMhKlOulF3Q"
SECOND_CREATED = "Tj9tw9WT2xqs9D"  # arrives last of the recorded deliveries, after `refund_twice`
SECOND_PROCESSED = "Tj9tzY1GfR6cQF"
ROW = "SELECT payment_id, status, refunded_minor, row_version FROM payments"


def row(status: str, refunded_minor: int, row_version: int) -> list[dict[str, Any]]:
    """What `ROW` reads when the payment's is the only row, in this state."""
    return [
        {
            "payment_id": PAYMENT,
            "status": status,
            "refunded_minor": refunded_minor,
            "row_version": row_version,
        }
    ]


def on_tab(scenario: Scenario, title: str) -> list[dict[str, Any]]:
    """Each row of the tab below its header, by column header."""
    header, *rows = scenario.sheets.tab(title).values()
    return [dict(zip(header, row, strict=False)) for row in rows]


def fetches(scenario: Scenario) -> int:
    """How many times Razorpay has been asked for the payment."""
    path = f"/v1/payments/{PAYMENT}"
    calls = cast(list[respx.models.Call], list(scenario.router.calls))
    return sum(1 for call in calls if call.request.url.path == path)


async def late(
    scenario: Scenario, client: httpx.AsyncClient, read_rows: ReadRows, event_id: str
) -> list[dict[str, Any]]:
    """A recorded delivery arrives late, carrying a state older than the row. It is a new
    event, so the payment is fetched once more; the ledger's rows after."""
    asked = fetches(scenario)
    [sent] = await scenario.razorpay.deliver(client, scenario.razorpay.delivery(event_id))
    await scenario.settle()
    assert (sent.status_code, sent.answer["result"]) == (202, "new")
    assert fetches(scenario) == asked + 1
    return await read_rows(ROW)


async def refund_twice(
    scenario: Scenario, client: httpx.AsyncClient, read_rows: ReadRows
) -> list[list[dict[str, Any]]]:
    """The captured payment is refunded twice as recorded, with its deliveries out of order
    and the second refund's `refund.created` still to come. The ledger's rows after the first
    refund's `refund.processed`, after the late `payment.captured`, after the second refund,
    and after the late `payment.authorized`."""
    razorpay = scenario.razorpay
    razorpay.refund(recorded_refund(FIRST, "pending"))
    razorpay.refund(recorded_refund(FIRST))
    await razorpay.deliver(client, razorpay.delivery(FIRST_PROCESSED))
    await scenario.settle()
    once = await read_rows(ROW)
    after_capture = await late(scenario, client, read_rows, CAPTURE)

    razorpay.refund(recorded_refund(SECOND, "pending"))
    razorpay.refund(recorded_refund(SECOND))
    for event_id in (SECOND_PROCESSED, FIRST_CREATED):
        await razorpay.deliver(client, razorpay.delivery(event_id))
    await scenario.settle()
    twice = await read_rows(ROW)
    after_authorisation = await late(scenario, client, read_rows, AUTHORISATION)
    return [once, after_capture, twice, after_authorisation]


async def test_a_partial_refund_shows_on_the_payments_own_row_and_in_the_daily_summary(
    scenario: Scenario, read_rows: ReadRows
) -> None:
    razorpay = scenario.razorpay
    razorpay.before_refunds(PAYMENT)
    older = [
        json.loads(razorpay.delivery(event_id).body)["payload"]["payment"]["entity"]
        for event_id in (CAPTURE, AUTHORISATION)
    ]

    async with scenario.running() as client:
        rows = await refund_twice(scenario, client, read_rows)
        last = await late(scenario, client, read_rows, SECOND_CREATED)

    # The late deliveries say less than the row did when they came; the row stayed as it was.
    assert [(payment["status"], payment["amount_refunded"]) for payment in older] == [
        ("captured", 0),
        ("authorized", 0),
    ]
    assert rows == [
        row("partially_refunded", 10000, 1),
        row("partially_refunded", 10000, 1),
        row("partially_refunded", 20000, 2),
        row("partially_refunded", 20000, 2),
    ]
    assert last == row("partially_refunded", 20000, 2)
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
    # All six deliveries are stored, each once: none was a duplicate of another.
    assert await read_rows(
        "SELECT type, count(*) AS n FROM events WHERE origin = 'delivery' GROUP BY type"
        " ORDER BY type"
    ) == [
        {"type": "payment.authorized", "n": 1},
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
        pending = await late(scenario, client, read_rows, SECOND_CREATED)

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
    assert refunded == row("partially_refunded", 20000, 2)
    [on_sheet] = on_tab(scenario, PAYMENTS)
    assert (on_sheet["Status"], on_sheet["Refunded"]) == ("partially_refunded", 200)
    [day] = on_tab(scenario, DAILY_SUMMARY)
    assert (day["Payments"], day["Gross"], day["Refunds"]) == (1, 500, 200)
    assert await read_rows(
        "SELECT type FROM events WHERE event_id LIKE 'unrecorded-%' ORDER BY type"
    ) == [{"type": "refund.created"}, {"type": "refund.failed"}]
    await scenario.check()


@pytest.mark.unrecorded
async def test_partial_refunds_adding_up_to_the_amount_show_the_row_refunded(
    scenario: Scenario, read_rows: ReadRows
) -> None:
    razorpay = scenario.razorpay
    razorpay.before_refunds(PAYMENT)

    async with scenario.running() as client:
        await refund_twice(scenario, client, read_rows)
        rest = razorpay.documented_refund(PAYMENT, amount=30000, sample="refund.processed")
        razorpay.refund(rest)
        for event_type in ("refund.processed", "refund.created"):
            delivery = razorpay.documented_delivery(event_type, rest["id"])
            await razorpay.deliver(client, delivery)
        await scenario.settle()
        refunded = await read_rows(ROW)
        # The second refund's late delivery still says 200 refunded of a captured payment.
        last = await late(scenario, client, read_rows, SECOND_CREATED)

    assert razorpay.fetches[PAYMENT]["status"] == "refunded"
    assert refunded == row("refunded", 50000, 3)
    assert last == refunded
    assert await read_rows("SELECT source_status FROM payments") == [{"source_status": "refunded"}]
    [on_sheet] = on_tab(scenario, PAYMENTS)
    assert (on_sheet["Status"], on_sheet["Amount"], on_sheet["Refunded"]) == ("refunded", 500, 500)
    # A refunded payment still counts in its day: it succeeded (G15).
    [day] = on_tab(scenario, DAILY_SUMMARY)
    assert (day["Payments"], day["Gross"], day["Refunds"]) == (1, 500, 500)
    await scenario.check()
