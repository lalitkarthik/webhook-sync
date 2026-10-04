"""Scenario: an outage and the catch-up. Payments made while the server was down come back
through the sweep, marked Via = `sweep` (testing.md, "Scenarios"; user story 13; H1, B7).

The server runs once, so the sweeper's checkpoints stand, and stops. Razorpay goes on making
payments whose deliveries never arrive; the server starts again and its start-up sweep stores
them. A delivery Razorpay retries after that is a new event that changes nothing (Q145). Seam:
the scenarios' harness.
"""

from datetime import datetime

from fakes.razorpay import recorded_listed_payments
from scenarios.harness import NOW, Scenario
from support import ReadRows

from webhook_sync.sinks.sheets import PAYMENTS

BEFORE = datetime(2026, 10, 2, 15, 30, tzinfo=NOW.tzinfo)
"""The server's last run before the outage: before every recorded payment was created."""
CAPTURE = "Tj9sMcl7hE9PaT"  # payment.captured of pay_Tj9rKBOXWcsMeE
FAILURE = "Tj9sobwBnTgcRB"  # payment.failed of pay_Tj9sm13R8h2378
UNDELIVERED = ("pay_Tj64grZUMO3PEj", "pay_Tj66x2JuEXjIdB")
"""Captured during the outage; P0 recorded no delivery of either."""
OUTAGE = ("pay_Tj64grZUMO3PEj", "pay_Tj66x2JuEXjIdB", "pay_Tj9rKBOXWcsMeE", "pay_Tj9sm13R8h2378")


async def ran_once(scenario: Scenario, moment: datetime) -> None:
    """The server started at `moment` and stopped once its sweeper set both checkpoints."""
    scenario.at(moment)
    async with scenario.running():
        await scenario.caught_up()
        await scenario.settle()


def sheet_rows(scenario: Scenario) -> dict[str, dict[str, str]]:
    """The Payments tab's rows by payment id, each as header → value."""
    header, *rows = scenario.sheets.tab(PAYMENTS).values()
    return {row[0]: dict(zip(header, row, strict=False)) for row in rows}


async def test_payments_made_while_the_server_was_down_come_back_through_the_sweep_once_each(
    scenario: Scenario, read_rows: ReadRows
) -> None:
    razorpay = scenario.razorpay
    await ran_once(scenario, BEFORE)
    capture = razorpay.delivery(CAPTURE)
    razorpay.never(capture)
    razorpay.never(razorpay.delivery(FAILURE))
    for payment_id in UNDELIVERED:
        razorpay.payments[payment_id] = recorded_listed_payments()[payment_id]
    # Every one of them is older than ten minutes and no delivery brought it (Q149).
    scenario.on_purpose.alerts.add("silent_webhook")

    scenario.at(NOW)
    async with scenario.running() as client:
        await scenario.caught_up()
        await scenario.settle()
        swept = sheet_rows(scenario)
        # Razorpay's own retry of one of them, after the sweep stored it.
        [late] = await razorpay.deliver(client, capture)
        await scenario.settle()

    assert sorted(swept) == sorted(OUTAGE)
    assert {row["Via"] for row in swept.values()} == {"sweep"}
    assert await read_rows(
        "SELECT payment_id, via, row_version FROM payments ORDER BY payment_id"
    ) == [{"payment_id": payment_id, "via": "sweep", "row_version": 1} for payment_id in OUTAGE]
    assert (late.status_code, late.answer) == (202, {"result": "new"})
    assert sheet_rows(scenario) == swept
    assert await read_rows(
        "SELECT origin, count(*) AS n FROM events GROUP BY origin ORDER BY origin"
    ) == [{"origin": "delivery", "n": 1}, {"origin": "sweep", "n": len(OUTAGE)}]
    await scenario.check()
