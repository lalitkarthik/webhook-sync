"""Scenario: Sheets answers 429, then recovers. A busy Google loses no row and writes none twice,
and Sync status says what happened (testing.md, "Scenarios"; user story 14; G2).

Google's write quota runs out while payments arrive: every write answers 429 until the quota
window ends, a minute after it started (recording 51). Their mirror jobs wait until then, as
the 429's body says, and the flush after it writes them. Seam: the scenarios' harness.
"""

import asyncio
import time
from datetime import timedelta
from typing import Any

import pytest
from fakes.sheets import Json
from invariants import SETTLE_LIMIT
from scenarios.harness import Scenario
from support import ReadRows

from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.sinks import sheets as sheets_module
from webhook_sync.sinks.sheets import PAYMENTS, SYNC_STATUS

FIRST = "Tj9h5SOW6XVf7g"  # payment.captured of pay_Tj9gmEfed6kRqO
DURING = ("Tj9sMcl7hE9PaT", "Tj9sobwBnTgcRB")
"""payment.captured of pay_Tj9rKBOXWcsMeE and payment.failed of pay_Tj9sm13R8h2378."""
PAYMENT_IDS = ["pay_Tj9gmEfed6kRqO", "pay_Tj9rKBOXWcsMeE", "pay_Tj9sm13R8h2378"]
WAITING = "Sheet jobs waiting"


@pytest.fixture(autouse=True)
def one_job_a_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    """A flush takes one job, so the jobs the 429 held back drain over flushes Sync status
    reports between."""
    monkeypatch.setattr(sheets_module, "FLUSH_LIMIT", 1)


def keys(scenario: Scenario) -> list[str]:
    return [row[0] for row in scenario.sheets.tab(PAYMENTS).values()[1:]]


def sync_statuses(scenario: Scenario) -> list[dict[str, Any]]:
    """What each write put on Sync status, label → value, in the order written."""
    tab = scenario.sheets.tab(SYNC_STATUS).sheet_id
    seen: list[dict[str, Any]] = []
    for requests in scenario.sheets.written:
        for request in requests:
            update: Json = request.get("updateCells", {})
            if update.get("start", {}) == {"sheetId": tab, "rowIndex": 1, "columnIndex": 0}:
                seen.append({_value(row[0]): _value(row[1]) for row in _cells(update)})
    return seen


def _cells(update: Json) -> list[list[Json]]:
    return [row["values"] for row in update["rows"]]


def _value(cell: Json) -> Any:
    return next(iter(cell.get("userEnteredValue", {}).values()), None)


async def retrying(read_rows: ReadRows, n: int) -> None:
    """Wait until `n` mirror jobs have failed once as `retry` and wait again."""
    deadline = time.monotonic() + SETTLE_LIMIT
    query = (
        "SELECT count(*) AS n FROM jobs WHERE stage = 'mirror' AND status = 'waiting'"
        " AND last_error_class = 'retry'"
    )
    while (await read_rows(query))[0]["n"] < n:
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {n} mirror jobs to retry")
        await asyncio.sleep(0.01)


async def test_a_sheets_429_is_retried_and_every_payment_appears_once_when_it_recovers(
    scenario: Scenario, read_rows: ReadRows
) -> None:
    razorpay, sheets = scenario.razorpay, scenario.sheets
    async with scenario.running() as client:
        await razorpay.deliver(client, razorpay.delivery(FIRST))
        await scenario.settle()
        quota_from = utc_now().replace(microsecond=0)  # the body names whole seconds
        sheets.exhaust_quota("write", quota_from)
        for event_id in DURING:
            await razorpay.deliver(client, razorpay.delivery(event_id))
        await retrying(read_rows, len(DURING))
        held = keys(scenario)
        refused = await read_rows(
            "SELECT run_after FROM jobs WHERE stage = 'mirror' AND status = 'waiting'"
        )

        scenario.at(utc_now() + timedelta(seconds=61))
        await scenario.settle()

    assert held == ["pay_Tj9gmEfed6kRqO"]
    # Each waited out the quota window, as the 429's body set it (no Retry-After).
    window_ends = to_db_text(quota_from + timedelta(seconds=60))
    assert all(row["run_after"] >= window_ends for row in refused)
    assert sorted(keys(scenario)) == PAYMENT_IDS
    assert await read_rows(
        "SELECT payment_key, attempts FROM jobs WHERE stage = 'mirror' ORDER BY payment_key"
    ) == [
        {"payment_key": "pay_Tj9gmEfed6kRqO", "attempts": 1},
        {"payment_key": "pay_Tj9rKBOXWcsMeE", "attempts": 2},
        {"payment_key": "pay_Tj9sm13R8h2378", "attempts": 2},
    ]
    # The first flush after the 429 says one retried job is still waiting; the next, none.
    statuses = sync_statuses(scenario)
    assert [status[WAITING] for status in statuses] == [0, 1, 0]
    assert {status["Sheet state"] for status in statuses} == {"active"}
    assert statuses[-1]["Rows written (total)"] == len(PAYMENT_IDS)
    await scenario.check()
