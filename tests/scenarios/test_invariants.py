"""The nine invariants (testing.md): each holds on a settled, healthy run, and each fails, naming
what it found, when a test breaks its promise on purpose.

Seam: the scenarios' harness. A healthy run delivers one recorded capture and settles; each test
then breaks one promise the way the world or a bug would (a person's edit, a write straight to
the database, Razorpay moving on) and runs that invariant's check.
"""

import asyncio
import time
from typing import Any

import pytest
from invariants import (
    assert_invariants,
    buyers_cells_untouched,
    every_event_stored_once,
    ledger_equals_source,
    no_false_conflict,
    no_surprise_dead_job,
    nothing_moves_backwards,
    nothing_stuck,
    quiet_alerts,
    sheet_equals_ledger,
)
from scenarios.harness import Scenario
from support import ReadRows, execute_aside, recorded_api_answer

from webhook_sync.db.schema import events

CAPTURE = "Tj9sMcl7hE9PaT"  # payment.captured of pay_Tj9rKBOXWcsMeE
AUTHORIZE = "Tj9rs3245YOiVJ"  # payment.authorized of the same payment
CAPTURED = "pay_Tj9rKBOXWcsMeE"
AUTHORIZED_STATE = "20261003T010947-pay_Tj9rKBOXWcsMeE-authorized"


@pytest.fixture
async def healthy(scenario: Scenario) -> Scenario:
    """One recorded capture delivered twice, settled, the app stopped."""
    async with scenario.running() as client:
        await scenario.razorpay.deliver(client, scenario.razorpay.delivery(CAPTURE), times=2)
        await scenario.settle()
    return scenario


async def test_every_invariant_holds_on_a_settled_healthy_run(healthy: Scenario) -> None:
    outcome = await healthy.outcome()

    assert outcome.payments
    assert outcome.deliveries
    await healthy.check()


async def test_a_broken_promise_fails_the_check_naming_the_invariant(healthy: Scenario) -> None:
    await execute_aside(healthy.data_dir, "UPDATE jobs SET status = 'dead', last_error = 'boom'")

    with pytest.raises(AssertionError) as failed:
        assert_invariants(await healthy.outcome(), healthy.on_purpose)

    assert "invariant 8, no surprise dead job: job 1 (ledger, pay_Tj9rKBOXWcsMeE) dead: boom" in (
        str(failed.value)
    )


async def test_1_the_ledger_equals_the_source(healthy: Scenario) -> None:
    moved_on = healthy.razorpay.fetches[CAPTURED] | {"fee": 1298, "tax": 198}
    healthy.razorpay.change(CAPTURED, moved_on)

    found = ledger_equals_source(await healthy.outcome(), healthy.on_purpose)

    assert found == [
        "pay_Tj9rKBOXWcsMeE differs from Razorpay's current state in fee_minor, fee_tax_minor,"
        " net_minor"
    ]


async def test_1_no_row_for_a_payment_razorpay_does_not_know(healthy: Scenario) -> None:
    del healthy.razorpay.payments[CAPTURED]

    found = ledger_equals_source(await healthy.outcome(), healthy.on_purpose)

    assert found == ["pay_Tj9rKBOXWcsMeE has a ledger row, but Razorpay doesn't know it"]


async def test_2_the_sheet_equals_the_ledger(healthy: Scenario) -> None:
    payments_tab = healthy.sheets.tab("Payments")
    header = payments_tab.values()[0]
    status = header.index("Status")
    healthy.sheets.apply(
        [
            {
                "updateCells": {
                    "rows": [{"values": [{"userEnteredValue": {"stringValue": "refunded"}}]}],
                    "fields": "userEnteredValue",
                    "start": {
                        "sheetId": payments_tab.sheet_id,
                        "rowIndex": 1,
                        "columnIndex": status,
                    },
                }
            }
        ]
    )

    found = sheet_equals_ledger(await healthy.outcome(), healthy.on_purpose)

    assert found == ["pay_Tj9rKBOXWcsMeE differs on the sheet in Status"]


async def test_2_a_payment_on_two_rows(healthy: Scenario) -> None:
    payments_tab = healthy.sheets.tab("Payments")
    row = [payments_tab.cell(1, column) for column in range(payments_tab.column_count)]
    healthy.sheets.apply(
        [
            {
                "appendCells": {
                    "sheetId": payments_tab.sheet_id,
                    "rows": [{"values": row}],
                    "fields": "*",
                }
            }
        ]
    )

    found = sheet_equals_ledger(await healthy.outcome(), healthy.on_purpose)

    assert found == ["pay_Tj9rKBOXWcsMeE is on rows 2, 3"]


async def test_3_every_delivery_is_logged_exactly_once(healthy: Scenario) -> None:
    await execute_aside(healthy.data_dir, "DELETE FROM deliveries WHERE result = 'duplicate'")

    found = every_event_stored_once(await healthy.outcome(), healthy.on_purpose)

    assert found == ["2 deliveries answered (1 duplicate, 1 new), but 1 logged (1 new)"]


async def test_3_every_event_is_stored_once(healthy: Scenario) -> None:
    # The same event again under a fresh id, its bytes resent differently: a second row.
    copy = {"event_id": "'Tj9sCopy000001'", "body_sha256": "'resent with other bytes'"}
    columns = [c.name for c in events.columns]
    copied = ", ".join(copy.get(c, c) for c in columns)
    await execute_aside(
        healthy.data_dir,
        f"INSERT INTO events ({', '.join(columns)}) SELECT {copied} FROM events",
    )

    found = every_event_stored_once(await healthy.outcome(), healthy.on_purpose)

    assert found == [
        "events Tj9sCopy000001, Tj9sMcl7hE9PaT are one event stored twice (same fingerprint)"
    ]


async def test_4_nothing_is_stuck(healthy: Scenario) -> None:
    await execute_aside(
        healthy.data_dir,
        "UPDATE jobs SET status = 'dead', last_error = NULL WHERE stage = 'mirror'",
    )
    await execute_aside(healthy.data_dir, "UPDATE jobs SET status = 'running' WHERE id = 1")

    found = nothing_stuck(await healthy.outcome(), healthy.on_purpose)

    assert found == [
        "job 1 (ledger, pay_Tj9rKBOXWcsMeE) running",
        "job 2 (mirror sheets, pay_Tj9rKBOXWcsMeE) dead with no reason",
    ]


async def until_seen(scenario: Scenario, payment_id: str, row_version: int) -> None:
    deadline = time.monotonic() + 10
    while row_version not in scenario.watch.seen.get(payment_id, {}):
        assert time.monotonic() < deadline, f"the watch never saw {payment_id} v{row_version}"
        await asyncio.sleep(0.01)


async def test_5_nothing_moves_backwards(scenario: Scenario, read_rows: ReadRows) -> None:
    razorpay = scenario.razorpay
    captured_state = razorpay.fetches[CAPTURED]
    razorpay.fetches[CAPTURED] = recorded_api_answer(AUTHORIZED_STATE)
    async with scenario.running() as client:
        await razorpay.deliver(client, razorpay.delivery(AUTHORIZE))
        await scenario.settle()
        [authorized] = await read_rows(f"SELECT * FROM payments WHERE payment_id = '{CAPTURED}'")
        razorpay.change(CAPTURED, captured_state)
        await razorpay.deliver(client, razorpay.delivery(CAPTURE))
        await scenario.settle()
        assert nothing_moves_backwards(await scenario.outcome(), scenario.on_purpose) == []

        older = {k: v for k, v in authorized.items() if k not in ("row_version", "updated_at")}
        assignments = ", ".join(f"{name} = {sql_literal(value)}" for name, value in older.items())
        await execute_aside(
            scenario.data_dir,
            f"UPDATE payments SET {assignments}, row_version = row_version + 1",
        )
        await until_seen(scenario, CAPTURED, 3)

    found = nothing_moves_backwards(await scenario.outcome(), scenario.on_purpose)

    assert found == [
        "pay_Tj9rKBOXWcsMeE went back: row version 3 holds Razorpay's state 1 of 2, after row"
        " version 2 held state 2"
    ]


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


async def test_6_nothing_of_the_buyers_is_touched(healthy: Scenario) -> None:
    healthy.sheets.apply(
        [
            {
                "updateCells": {
                    "rows": [{"values": [{"userEnteredValue": {"stringValue": "ours now"}}]}],
                    "fields": "userEnteredValue",
                    "start": {"sheetId": 0, "rowIndex": 1, "columnIndex": 2},
                }
            }
        ]
    )

    found = buyers_cells_untouched(await healthy.outcome(), healthy.on_purpose)

    assert found == ["the buyer's tab Sheet1: C2 changed"]


async def test_7_no_false_conflict(healthy: Scenario) -> None:
    await execute_aside(
        healthy.data_dir, "UPDATE deliveries SET result = 'conflict' WHERE result = 'duplicate'"
    )

    found = no_false_conflict(await healthy.outcome(), healthy.on_purpose)

    assert found == [
        "Tj9sMcl7hE9PaT recorded as a conflict, but no other event was sent under its id on purpose"
    ]


async def test_8_no_surprise_dead_job(healthy: Scenario) -> None:
    await execute_aside(
        healthy.data_dir,
        "UPDATE jobs SET status = 'dead', last_error = 'Razorpay answered HTTP 400'"
        " WHERE stage = 'mirror'",
    )

    found = no_surprise_dead_job(await healthy.outcome(), healthy.on_purpose)
    healthy.on_purpose.dead.add(CAPTURED)
    declared = no_surprise_dead_job(await healthy.outcome(), healthy.on_purpose)

    assert found == ["job 2 (mirror sheets, pay_Tj9rKBOXWcsMeE) dead: Razorpay answered HTTP 400"]
    assert declared == []


async def test_9_quiet_alerts(healthy: Scenario) -> None:
    await execute_aside(
        healthy.data_dir,
        "INSERT INTO alerts (kind, last_sent_at, suppressed_count)"
        " VALUES ('dead_jobs', '2026-10-02T20:05:01.000000Z', 2)",
    )

    found = quiet_alerts(await healthy.outcome(), healthy.on_purpose)
    healthy.on_purpose.alerts.add("dead_jobs")
    declared = quiet_alerts(await healthy.outcome(), healthy.on_purpose)

    assert found == ["alert dead_jobs fired (and 2 more suppressed)"]
    assert declared == []
