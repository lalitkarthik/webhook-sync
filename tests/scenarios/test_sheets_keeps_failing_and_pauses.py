"""Scenario: Sheets keeps failing, the sink pauses, and `wsync resume` writes every row once
(testing.md, "Scenarios"; G2, G11; Q134).

Google's write quota runs out while payments arrive, and before the quota window ends the buyer
stops sharing the spreadsheet: the retried flush is answered 403, a pause. `wsync status` says
the sheet is paused and why, one `paused` alert goes out, and the mirror jobs wait. The buyer
shares it again and resumes; the next flush writes them. Seam: the scenarios' harness.
"""

import asyncio
import time
from datetime import timedelta
from typing import Any, cast

import respx
from invariants import SETTLE_LIMIT
from scenarios.harness import HOOK, Scenario
from scenarios.test_sheets_429_and_recovery import DURING, FIRST, PAYMENT_IDS, keys, retrying
from support import ReadRows

from webhook_sync.clock import utc_now

UNSHARED = "Sheets answered HTTP 403: The caller does not have permission"


async def paused(scenario: Scenario, name: str) -> dict[str, Any]:
    """Wait until `wsync status` shows `name` paused; its line there."""
    deadline = time.monotonic() + SETTLE_LIMIT
    while True:
        [line] = [sink for sink in (await scenario.status())["sinks"] if sink["name"] == name]
        if line["state"] == "paused":
            return line
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {name} to pause")
        await asyncio.sleep(0.01)


async def test_sheets_keeps_failing_until_the_sink_pauses_and_resume_writes_every_row_once(
    scenario: Scenario, read_rows: ReadRows
) -> None:
    razorpay, sheets = scenario.razorpay, scenario.sheets
    async with scenario.running() as client:
        await razorpay.deliver(client, razorpay.delivery(FIRST))
        await scenario.settle()
        sheets.exhaust_quota("write", utc_now().replace(microsecond=0))
        for event_id in DURING:
            await razorpay.deliver(client, razorpay.delivery(event_id))
        await retrying(read_rows, len(DURING))
        sheets.access = "none"
        scenario.on_purpose.paused.add("sheets")
        scenario.on_purpose.alerts.add("paused")
        scenario.at(utc_now() + timedelta(seconds=61))

        line = await paused(scenario, "sheets")
        await scenario.settle()
        status = await scenario.status()
        held = keys(scenario)

        sheets.access = "edit"
        await scenario.resume("sheets")
        await scenario.settle()
        resumed = await scenario.status()

    assert line["reason"] == UNSHARED
    assert line["since"] is not None
    assert status["jobs"]["mirror"] == {"waiting": len(DURING), "dead": 0}
    assert held == ["pay_Tj9gmEfed6kRqO"]
    # One alert said so, and resuming sent no other.
    calls = cast(list[respx.models.Call], list(scenario.router.calls))
    sent = [call.request.content.decode() for call in calls if str(call.request.url) == HOOK]
    assert len(sent) == 1
    assert f"sink sheets paused: {UNSHARED}" in sent[0]
    assert await read_rows("SELECT kind, suppressed_count FROM alerts") == [
        {"kind": "paused", "suppressed_count": 0}
    ]
    assert [sink["state"] for sink in resumed["sinks"]] == ["active", "active"]
    assert resumed["jobs"]["mirror"] == {"waiting": 0, "dead": 0}
    assert sorted(keys(scenario)) == PAYMENT_IDS
    assert scenario.on_purpose.paused == set()
    await scenario.check()
