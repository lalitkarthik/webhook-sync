"""Scenario: a duplicate storm. One event delivered many times makes one row (testing.md,
"Scenarios"; user story 12).

Razorpay retries a delivery it thinks failed, byte for byte (G38), and anyone holding a captured
delivery can replay it under a fresh `x-razorpay-event-id`, which isn't signed (F12): the
signed-body hash catches those (Q86). A different event under a known id is a conflict, kept
for a person to look at (F02). Seam: the scenarios' harness.
"""

import json

from scenarios.harness import Scenario
from support import ReadRows

from webhook_sync.sinks.sheets import PAYMENTS

CAPTURE = "Tj9sMcl7hE9PaT"  # payment.captured of pay_Tj9rKBOXWcsMeE, recorded byte-exact
CAPTURED = "pay_Tj9rKBOXWcsMeE"
RETRIES = 30
REPLAYS = ("Tj9sReplay0001", "Tj9sReplay0002", "Tj9sReplay0003")
REPLAYED = 5
"""Each replayed id is sent this many times."""


async def test_one_delivery_sent_many_times_makes_one_event_one_row_and_one_sheet_row(
    scenario: Scenario, read_rows: ReadRows
) -> None:
    razorpay = scenario.razorpay
    capture = razorpay.delivery(CAPTURE)

    async with scenario.running() as client:
        sent = await razorpay.deliver(client, capture, times=RETRIES)
        for event_id in REPLAYS:
            sent += await razorpay.deliver(client, capture.under(event_id), times=REPLAYED)
        await scenario.settle()

    total = RETRIES + len(REPLAYS) * REPLAYED
    assert sorted(s.status_code for s in sent) == [200] * (total - 1) + [202]
    assert await read_rows("SELECT event_id, payment_key FROM events") == [
        {"event_id": CAPTURE, "payment_key": CAPTURED}
    ]
    assert await read_rows("SELECT payment_id, row_version FROM payments") == [
        {"payment_id": CAPTURED, "row_version": 1}
    ]
    _, *rows = scenario.sheets.tab(PAYMENTS).values()
    assert [row[0] for row in rows] == [CAPTURED]
    assert await read_rows(
        "SELECT event_id, result, count(*) AS n FROM deliveries GROUP BY result ORDER BY result"
    ) == [
        {"event_id": CAPTURE, "result": "duplicate", "n": total - 1},
        {"event_id": CAPTURE, "result": "new", "n": 1},
    ]
    await scenario.check()


async def test_a_different_body_under_a_known_event_id_is_a_conflict_accepted_only_on_purpose(
    scenario: Scenario, read_rows: ReadRows
) -> None:
    razorpay = scenario.razorpay
    capture = razorpay.delivery(CAPTURE)
    other = json.loads(capture.body)
    other["created_at"] += 60  # another event: its fingerprint differs
    conflicting = capture.with_body(other)

    async with scenario.running() as client:
        await razorpay.deliver(client, capture, times=3)
        [sent] = await razorpay.deliver(client, conflicting)
        await scenario.settle()

    assert (sent.status_code, sent.answer) == (200, {"result": "conflict"})
    assert await read_rows("SELECT result FROM deliveries WHERE result = 'conflict'") == [
        {"result": "conflict"}
    ]
    assert await read_rows("SELECT count(*) AS n FROM payments") == [{"n": 1}]
    undeclared = await failure_of(scenario)
    assert undeclared == (
        "invariant 7, no false conflict: Tj9sMcl7hE9PaT recorded as a conflict, but no other"
        " event was sent under its id on purpose"
    )
    scenario.on_purpose.conflicts.add(CAPTURE)
    await scenario.check()


async def failure_of(scenario: Scenario) -> str | None:
    try:
        await scenario.check()
    except AssertionError as failed:
        return str(failed)
    return None
