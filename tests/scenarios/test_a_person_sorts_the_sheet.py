"""Scenario: a person sorts the Payments tab between the flusher's read and its write. It is
caught and repaired, and each payment appears once (testing.md, "Scenarios"; G5; Q63).

Updates never write the key cell, so an update the sort sent onto another payment's row shows
up in the post-write check: both payments are queued again, one `sheet_changed` alert says so,
and the next flush rewrites them by key. Seam: the scenarios' harness.
"""

from fakes.sheets import sort_rows
from scenarios.harness import Scenario
from support import ReadRows

from webhook_sync.sinks.sheets import PAYMENTS

DELIVERED = ("Tj9h5SOW6XVf7g", "Tj9sobwBnTgcRB", "Tj9sMcl7hE9PaT")
"""The captures of pay_Tj9gmEfed6kRqO and pay_Tj9rKBOXWcsMeE and the failure of
pay_Tj9sm13R8h2378, in that order: the Payments rows, top to bottom."""
CHANGED = "pay_Tj9rKBOXWcsMeE"
ANNOUNCED = "Tj9rs3245YOiVJ"  # payment.authorized of pay_Tj9rKBOXWcsMeE, a new event here


def sheet_rows(scenario: Scenario) -> list[dict[str, str]]:
    header, *rows = scenario.sheets.tab(PAYMENTS).values()
    return [dict(zip(header, row, strict=False)) for row in rows]


async def test_a_sort_between_the_flushers_read_and_write_is_caught_and_repaired(
    scenario: Scenario, read_rows: ReadRows
) -> None:
    razorpay, sheets = scenario.razorpay, scenario.sheets
    async with scenario.running() as client:
        for event_id in DELIVERED:
            await razorpay.deliver(client, razorpay.delivery(event_id))
            await scenario.settle()
        before = [row["Payment ID"] for row in sheet_rows(scenario)]
        # The payment's description changes at Razorpay, and an event announces it.
        razorpay.change(CHANGED, razorpay.fetches[CHANGED] | {"description": "Plan, renewed"})
        payments = sheets.tab(PAYMENTS).sheet_id
        sheets.after_next("getByDataFilter", [sort_rows(payments, order="DESCENDING")])
        await razorpay.deliver(client, razorpay.delivery(ANNOUNCED))
        scenario.on_purpose.alerts.add("sheet_changed")
        await scenario.settle()

    assert before == ["pay_Tj9gmEfed6kRqO", "pay_Tj9sm13R8h2378", "pay_Tj9rKBOXWcsMeE"]
    rows = sheet_rows(scenario)
    # Sorted by the person, then each row rewritten in place by its key.
    assert [row["Payment ID"] for row in rows] == [
        "pay_Tj9sm13R8h2378",
        "pay_Tj9rKBOXWcsMeE",
        "pay_Tj9gmEfed6kRqO",
    ]
    assert [row["Description"] for row in rows if row["Payment ID"] == CHANGED] == ["Plan, renewed"]
    [alert] = await read_rows("SELECT kind, last_sent_at FROM alerts WHERE kind = 'sheet_changed'")
    assert alert["last_sent_at"] is not None
    assert await read_rows("SELECT state, moved_in_a_row FROM sinks WHERE name = 'sheets'") == [
        {"state": "active", "moved_in_a_row": 0}
    ]
    await scenario.check()
