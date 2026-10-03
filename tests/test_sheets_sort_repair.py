"""A sort during a flush is caught and repaired (seam: the flusher on the runner).

After every write the flusher re-reads the keys of the Payments rows it wrote. A person who
sorts the tab between the flusher's read and its write sends an update onto another payment's
row; the check sees a key it did not expect, queues both payments again, alerts
`sheet_changed`, and counts it in `sinks.moved_in_a_row`. A second in a row pauses the sink
"sheet changed during write" until `wsync resume`; a clean flush resets the count (A14).
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from fakes.sheets import Json
from support import ReadRows
from test_sheets_flusher import NOW, Sheets, captured, flusher_on, put
from time_machine import TimeMachineFixture

from webhook_sync.clock import utc_now
from webhook_sync.config import Config
from webhook_sync.db.writer import Writer
from webhook_sync.queue.pause import resume
from webhook_sync.sinks.sheets import SHEET_CHANGED

DESCRIPTION = 13


@pytest.fixture(autouse=True)
def clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(NOW, tick=False)
    return time_machine


@pytest.fixture
def columns_config(config: Config) -> Config:
    return replace(config, razorpay=replace(config.razorpay, metadata_keys=("order_id",)))


@pytest.fixture
async def sheets(writer: Writer, columns_config: Config) -> AsyncIterator[Sheets]:
    """A sheet holding pay_00, pay_01 and pay_02, each with its own description."""
    async with flusher_on(writer, columns_config) as sheets:
        for n in range(3):
            await put(sheets.writer, captured(f"pay_{n:02d}", description=f"Plan {n}"))
        await sheets.flush()
        yield sheets


def sort(sheets: Sheets, order: str) -> Json:
    """A person's sort of the Payments rows by payment id."""
    return {
        "sortRange": {
            "range": {"sheetId": sheets.payments.sheet_id, "startRowIndex": 1},
            "sortSpecs": [{"dimensionIndex": 0, "sortOrder": order}],
        }
    }


def rows(sheets: Sheets) -> list[tuple[str, str]]:
    """Each Payments row's key and description, top to bottom."""
    return [(row[0], row[DESCRIPTION]) for row in sheets.payments.values()[1:]]


async def sink(read_rows: ReadRows) -> dict[str, Any]:
    found = await read_rows("SELECT state, reason, moved_in_a_row FROM sinks WHERE name = 'sheets'")
    return found[0] if found else {}


def changed_alerts(sheets: Sheets) -> list[str]:
    return [text for text in sheets.alerts() if "sheet_changed" in text]


async def sorted_during_the_flush(sheets: Sheets, order: str) -> None:
    """pay_00 changes; a person sorts the tab between the flusher's read and its write."""
    await put(sheets.writer, captured("pay_00", description="Plan 0, renewed"))
    sheets.fake.after_next("getByDataFilter", [sort(sheets, order)])
    await sheets.flush()


async def test_a_normal_flush_costs_two_reads_and_one_write(sheets: Sheets) -> None:
    await put(sheets.writer, captured("pay_01", description="Plan 1, renewed"))
    await put(sheets.writer, captured("pay_03", description="Plan 3"))
    sheets.fake.calls.clear()

    assert await sheets.flush() == 2

    assert sheets.fake.calls == ["getByDataFilter", "batchUpdate", "getByDataFilter"]


async def test_a_sort_between_read_and_write_is_repaired_on_the_next_flush_with_one_alert(
    sheets: Sheets, read_rows: ReadRows, clock: TimeMachineFixture
) -> None:
    await sorted_during_the_flush(sheets, "DESCENDING")

    # pay_00's update landed on row 1, which the sort gave to pay_02.
    assert rows(sheets)[0] == ("pay_02", "Plan 0, renewed")
    assert await sink(read_rows) == {"state": "active", "reason": None, "moved_in_a_row": 1}
    assert len(changed_alerts(sheets)) == 1
    assert "pay_00" in changed_alerts(sheets)[0]
    assert "pay_02" in changed_alerts(sheets)[0]
    waiting = await read_rows(
        "SELECT payment_key, last_error_class FROM jobs WHERE status = 'waiting' "
        "ORDER BY payment_key"
    )
    assert waiting == [
        {"payment_key": "pay_00", "last_error_class": "retry"},
        {"payment_key": "pay_02", "last_error_class": None},
    ]

    clock.move_to(NOW + timedelta(minutes=10))
    assert await sheets.flush() == 2

    assert rows(sheets) == [
        ("pay_02", "Plan 2"),
        ("pay_01", "Plan 1"),
        ("pay_00", "Plan 0, renewed"),
    ]
    assert await read_rows("SELECT id FROM jobs WHERE status != 'done'") == []
    assert await sink(read_rows) == {"state": "active", "reason": None, "moved_in_a_row": 0}
    assert len(changed_alerts(sheets)) == 1


async def test_a_sort_twice_in_a_row_pauses_until_resumed(
    sheets: Sheets, read_rows: ReadRows, clock: TimeMachineFixture
) -> None:
    await sorted_during_the_flush(sheets, "DESCENDING")
    clock.move_to(NOW + timedelta(minutes=10))
    sheets.fake.after_next("getByDataFilter", [sort(sheets, "ASCENDING")])

    await sheets.flush()

    assert await sink(read_rows) == {
        "state": "paused",
        "reason": SHEET_CHANGED,
        "moved_in_a_row": 2,
    }
    assert [text for text in sheets.alerts() if "paused" in text] == [
        f"webhook-sync paused: sink sheets paused: {SHEET_CHANGED}"
    ]
    assert await sheets.flush() == 0  # nothing is claimed while paused

    assert await resume(sheets.writer, "sheets", pauses=sheets.pauses, now=utc_now()) == "resumed"
    assert await sink(read_rows) == {"state": "active", "reason": None, "moved_in_a_row": 0}
    assert await sheets.flush() == 2

    assert rows(sheets) == [
        ("pay_00", "Plan 0, renewed"),
        ("pay_01", "Plan 1"),
        ("pay_02", "Plan 2"),
    ]
    assert await read_rows("SELECT id FROM jobs WHERE status != 'done'") == []


async def test_a_clean_flush_between_two_sorts_keeps_the_sink_running(
    sheets: Sheets, read_rows: ReadRows, clock: TimeMachineFixture
) -> None:
    await sorted_during_the_flush(sheets, "DESCENDING")
    clock.move_to(NOW + timedelta(minutes=10))
    await sheets.flush()  # the repair, clean

    clock.move_to(NOW + timedelta(minutes=20))
    await sorted_during_the_flush(sheets, "ASCENDING")

    assert await sink(read_rows) == {"state": "active", "reason": None, "moved_in_a_row": 1}
