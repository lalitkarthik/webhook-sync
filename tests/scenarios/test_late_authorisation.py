"""Scenario: a late authorisation. A payment that failed and was later authorised and captured
updates the same row (testing.md, "Scenarios"; user story 15; C13).

Razorpay can authorise a failed payment up to three days later. Here no event announces it: the
hourly re-list of the last 7 days of payments finds the new state (Q108, G53). The slices have
passed the payment, overlap and all, before it changes, so only the re-list can bring it. Seam:
the scenarios' harness.
"""

from datetime import datetime, timedelta

import pytest
from scenarios.harness import NOW, Scenario
from support import ReadRows

from webhook_sync.sinks.sheets import PAYMENTS
from webhook_sync.sweeper import sweep as sweeper_module

FAILURE = "Tj9sobwBnTgcRB"  # payment.failed of pay_Tj9sm13R8h2378, created 19:40:20
FAILED = "pay_Tj9sm13R8h2378"
STARTED = datetime(2026, 10, 2, 19, 30, tzinfo=NOW.tzinfo)
DELIVERED = datetime(2026, 10, 2, 19, 41, tzinfo=NOW.tzinfo)
PASSED = datetime(2026, 10, 2, 19, 55, tzinfo=NOW.tzinfo)
"""The slices' checkpoint is then 19:54, so the next slice re-reads from 19:44: past the
payment."""
RELISTED = STARTED + timedelta(hours=1, minutes=30)
"""The first re-list was at the first start; the next is due an hour on."""
ROW = "SELECT payment_id, status, source_status, row_version FROM payments"


@pytest.fixture(autouse=True)
def frequent_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sweeper_module, "SWEEP_EVERY", timedelta(milliseconds=300))


def sheet_rows(scenario: Scenario) -> list[dict[str, str]]:
    header, *rows = scenario.sheets.tab(PAYMENTS).values()
    return [dict(zip(header, row, strict=False)) for row in rows]


async def test_a_failed_payment_authorised_and_captured_later_updates_its_row_after_the_re_list(
    scenario: Scenario, read_rows: ReadRows
) -> None:
    razorpay = scenario.razorpay
    scenario.at(STARTED)
    async with scenario.running() as client:
        await scenario.caught_up()
        scenario.at(DELIVERED)
        await razorpay.deliver(client, razorpay.delivery(FAILURE))
        await scenario.settle()
        failed = await read_rows(ROW)
        scenario.at(PASSED)
        await scenario.caught_up()
        await scenario.settle()

        razorpay.authorise_late(FAILED)
        scenario.at(RELISTED)
        await scenario.relisted(RELISTED)
        await scenario.settle()

    assert failed == [
        {"payment_id": FAILED, "status": "failed", "source_status": "failed", "row_version": 1}
    ]
    [row] = await read_rows(ROW)
    assert (row["payment_id"], row["status"], row["source_status"]) == (
        FAILED,
        "succeeded",
        "captured",
    )
    assert [(r["Payment ID"], r["Status"]) for r in sheet_rows(scenario)] == [(FAILED, "succeeded")]
    # The capture came from the re-list: no delivery announced it.
    assert await read_rows("SELECT origin, type FROM events ORDER BY received_at, origin") == [
        {"origin": "delivery", "type": "payment.failed"},
        {"origin": "sweep", "type": "payment.failed"},
        {"origin": "sweep", "type": "payment.captured"},
    ]
    await scenario.check()
