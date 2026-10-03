"""Sheet trouble: every way the sheet can fail ends correctly (seam: the flusher on the runner).

Unshared, deleted or changed things pause the sink with their reason, stored in `sinks`, one
`paused` alert, and every job kept waiting with its attempt refunded; quota and a stale read
wait and retry; a deleted Sync status comes back; one bad cell drops only its row. The fake
answers with the bodies recorded live (Q141); no call reaches Google.
"""

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fakes.sheets import Json
from support import ReadRows
from test_sheets_flusher import (
    NOW,
    Sheets,
    captured,
    flusher_on,
    put,
    status_lines,
    statuses,
)
from time_machine import TimeMachineFixture

from webhook_sync.clock import utc_now
from webhook_sync.config import Config
from webhook_sync.db.schema import alerts
from webhook_sync.db.writer import Writer
from webhook_sync.queue.pause import resume
from webhook_sync.sinks.encoding import payment_columns
from webhook_sync.sinks.sheets import DAILY_SUMMARY, PAYMENTS, SYNC_STATUS


@pytest.fixture(autouse=True)
def clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(NOW, tick=False)
    return time_machine


@pytest.fixture
def columns_config(config: Config) -> Config:
    """The test config with one metadata key, `order_id`, as the flusher tests use."""
    return replace(config, razorpay=replace(config.razorpay, metadata_keys=("order_id",)))


@pytest.fixture
async def sheets(writer: Writer, columns_config: Config) -> AsyncIterator[Sheets]:
    async with flusher_on(writer, columns_config) as sheets:
        yield sheets


@pytest.fixture
async def flushed(sheets: Sheets) -> Sheets:
    """A sheet whose first flush set up the tabs and wrote pay_00."""
    await put(sheets.writer, captured("pay_00"))
    await sheets.flush()
    return sheets


async def sink(read_rows: ReadRows) -> dict[str, Any]:
    rows = await read_rows("SELECT state, reason FROM sinks WHERE name = 'sheets'")
    return rows[0] if rows else {}


async def waiting_jobs(read_rows: ReadRows) -> list[dict[str, Any]]:
    return await read_rows(
        "SELECT status, attempts, last_error_class FROM jobs "
        "WHERE stage = 'mirror' AND status != 'done' ORDER BY id"
    )


def keys(sheets: Sheets) -> list[str]:
    return [row[0] for row in sheets.payments.values()[1:]]


def paused_alerts(sheets: Sheets) -> list[str]:
    return [text for text in sheets.alerts() if "paused" in text]


def set_cell(sheet_id: int, row: int, column: int, text: str) -> Json:
    return {
        "updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": row, "columnIndex": column},
            "rows": [{"values": [{"userEnteredValue": {"stringValue": text}}]}],
            "fields": "userEnteredValue",
        }
    }


def rows_at(sheet_id: int, rows: list[list[str]]) -> Json:
    return {
        "updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
            "rows": [
                {"values": [{"userEnteredValue": {"stringValue": text}} for text in row]}
                for row in rows
            ],
            "fields": "userEnteredValue",
        }
    }


def row_count(sheet_id: int, rows: int) -> Json:
    return {
        "updateSheetProperties": {
            "properties": {"sheetId": sheet_id, "gridProperties": {"rowCount": rows}},
            "fields": "gridProperties.rowCount",
        }
    }


async def assert_paused_then_resumed(
    sheets: Sheets, read_rows: ReadRows, reason: str, *, keys_after: list[str]
) -> None:
    """Paused with `reason`, one alert, both jobs waiting with no attempt spent; once resumed
    (after the fix the caller made), they flush."""
    assert await sink(read_rows) == {"state": "paused", "reason": reason}
    assert len(paused_alerts(sheets)) == 1
    assert reason in paused_alerts(sheets)[0]
    assert await waiting_jobs(read_rows) == [
        {"status": "waiting", "attempts": 0, "last_error_class": "pause"},
        {"status": "waiting", "attempts": 0, "last_error_class": "pause"},
    ]
    assert await sheets.flush() == 0  # nothing is claimed while paused

    assert await resume(sheets.writer, "sheets", pauses=sheets.pauses, now=utc_now()) == "resumed"
    assert await sheets.flush() == 2
    assert await waiting_jobs(read_rows) == []
    assert keys(sheets) == keys_after


async def two_more(sheets: Sheets) -> None:
    await put(sheets.writer, captured("pay_01"))
    await put(sheets.writer, captured("pay_02"))


# -- pauses ---------------------------------------------------------------------------------------


async def test_an_unshared_spreadsheet_pauses_and_the_jobs_flush_after_resume(
    flushed: Sheets, read_rows: ReadRows
) -> None:
    await two_more(flushed)
    flushed.fake.access = "none"

    await flushed.flush()

    assert keys(flushed) == ["pay_00"]
    flushed.fake.access = "edit"
    reason = "Sheets answered HTTP 403: The caller does not have permission"
    await assert_paused_then_resumed(
        flushed, read_rows, reason, keys_after=["pay_00", "pay_01", "pay_02"]
    )


async def test_a_view_only_share_pauses_at_the_write(flushed: Sheets, read_rows: ReadRows) -> None:
    await two_more(flushed)
    flushed.fake.access = "view"

    await flushed.flush()

    flushed.fake.access = "edit"
    reason = "Sheets answered HTTP 403: The caller does not have permission"
    await assert_paused_then_resumed(
        flushed, read_rows, reason, keys_after=["pay_00", "pay_01", "pay_02"]
    )


async def test_a_revoked_credential_pauses(flushed: Sheets, read_rows: ReadRows) -> None:
    await two_more(flushed)
    flushed.fake.revoke_tokens()

    await flushed.flush()

    reason = (await sink(read_rows))["reason"]
    assert reason.startswith("Sheets answered HTTP 401: Request had invalid authentication")
    await assert_paused_then_resumed(
        flushed, read_rows, reason, keys_after=["pay_00", "pay_01", "pay_02"]
    )


async def test_a_refused_service_account_key_pauses(flushed: Sheets, read_rows: ReadRows) -> None:
    await two_more(flushed)
    flushed.fake.revoke_tokens()
    await flushed.flush()
    await resume(flushed.writer, "sheets", pauses=flushed.pauses, now=utc_now())
    flushed.hook.reset()
    async with flushed.writer.transaction() as tx:
        await tx.execute(sa.delete(alerts))  # a fresh hour for the next `paused` alert
    flushed.fake.refuse_tokens = True

    await flushed.flush()

    flushed.fake.refuse_tokens = False
    await assert_paused_then_resumed(
        flushed,
        read_rows,
        "Google refused the service-account key",
        keys_after=["pay_00", "pay_01", "pay_02"],
    )


async def test_a_deleted_spreadsheet_pauses_as_spreadsheet_missing(
    flushed: Sheets, read_rows: ReadRows
) -> None:
    await two_more(flushed)
    flushed.fake.deleted = True

    await flushed.flush()

    flushed.fake.deleted = False
    await assert_paused_then_resumed(
        flushed, read_rows, "spreadsheet missing", keys_after=["pay_00", "pay_01", "pay_02"]
    )


@pytest.mark.parametrize("title", [PAYMENTS, DAILY_SUMMARY])
async def test_a_deleted_payments_or_summary_tab_pauses_as_tab_missing(
    flushed: Sheets, read_rows: ReadRows, title: str
) -> None:
    await two_more(flushed)
    flushed.fake.apply([{"deleteSheet": {"sheetId": flushed.fake.tab(title).sheet_id}}])

    await flushed.flush()

    assert await sink(read_rows) == {"state": "paused", "reason": "tab missing"}
    assert len(paused_alerts(flushed)) == 1
    assert [j["attempts"] for j in await waiting_jobs(read_rows)] == [0, 0]
    assert not flushed.fake.has_tab(title)  # never recreated: the buyer's data was there


async def test_a_tab_deleted_between_the_read_and_the_write_pauses_as_tab_missing(
    flushed: Sheets, read_rows: ReadRows
) -> None:
    await two_more(flushed)
    flushed.fake.after_next(
        "getByDataFilter", [{"deleteSheet": {"sheetId": flushed.payments.sheet_id}}]
    )

    await flushed.flush()

    assert await sink(read_rows) == {"state": "paused", "reason": "tab missing"}
    assert [j["attempts"] for j in await waiting_jobs(read_rows)] == [0, 0]


async def test_a_renamed_column_pauses_as_layout_changed_and_nothing_is_written(
    flushed: Sheets, read_rows: ReadRows
) -> None:
    await two_more(flushed)
    tab = flushed.payments
    ours = tab.value(0, 1)
    flushed.fake.apply([set_cell(tab.sheet_id, 0, 1, "Type")])
    flushed.fake.calls.clear()

    await flushed.flush()

    assert "batchUpdate" not in flushed.fake.calls
    assert keys(flushed) == ["pay_00"]
    flushed.fake.apply([set_cell(tab.sheet_id, 0, 1, ours)])  # the buyer puts it back
    await assert_paused_then_resumed(
        flushed, read_rows, "layout changed", keys_after=["pay_00", "pay_01", "pay_02"]
    )


async def test_a_changed_daily_summary_header_pauses_as_layout_changed(
    flushed: Sheets, read_rows: ReadRows
) -> None:
    await two_more(flushed)
    tab = flushed.fake.tab(DAILY_SUMMARY)
    flushed.fake.apply([set_cell(tab.sheet_id, 0, 4, "Total")])

    await flushed.flush()

    assert await sink(read_rows) == {"state": "paused", "reason": "layout changed"}


async def test_a_column_added_to_the_right_of_ours_is_left_alone(
    flushed: Sheets, read_rows: ReadRows
) -> None:
    tab = flushed.payments
    width = len(tab.values()[0])
    flushed.fake.apply([set_cell(tab.sheet_id, 0, width, "My notes")])
    await two_more(flushed)

    await flushed.flush()

    assert await sink(read_rows) == {}
    assert keys(flushed) == ["pay_00", "pay_01", "pay_02"]
    assert flushed.payments.value(0, width) == "My notes"


async def test_a_full_spreadsheet_pauses_as_sheet_full(
    flushed: Sheets, read_rows: ReadRows
) -> None:
    tab = flushed.payments
    flushed.fake.apply([row_count(tab.sheet_id, 2)])
    flushed.fake.cell_limit = flushed.fake.cell_count()
    await two_more(flushed)

    await flushed.flush()

    assert keys(flushed) == ["pay_00"]
    flushed.fake.cell_limit *= 2
    await assert_paused_then_resumed(
        flushed, read_rows, "sheet full", keys_after=["pay_00", "pay_01", "pay_02"]
    )


# -- retries --------------------------------------------------------------------------------------


async def test_a_429_waits_until_the_quota_window_ends(
    flushed: Sheets, read_rows: ReadRows, clock: TimeMachineFixture
) -> None:
    await two_more(flushed)
    window_start = NOW - timedelta(seconds=10)
    flushed.fake.exhaust_quota("write", window_start)

    await flushed.flush()

    jobs = await read_rows(
        "SELECT status, last_error_class, run_after FROM jobs WHERE status != 'done' ORDER BY id"
    )
    assert [(j["status"], j["last_error_class"]) for j in jobs] == [("waiting", "retry")] * 2
    assert all(j["run_after"] >= "2026-10-02T16:00:50" for j in jobs)
    assert await sink(read_rows) == {}

    clock.move_to(NOW + timedelta(minutes=10))
    assert await flushed.flush() == 2
    assert keys(flushed) == ["pay_00", "pay_01", "pay_02"]


async def test_rows_deleted_after_the_read_retry_and_the_next_flush_reads_again(
    flushed: Sheets, read_rows: ReadRows, clock: TimeMachineFixture
) -> None:
    await two_more(flushed)
    await flushed.flush()
    await put(flushed.writer, captured("pay_02", description="Pro plan, renewed"))
    flushed.fake.after_next("getByDataFilter", [row_count(flushed.payments.sheet_id, 3)])

    await flushed.flush()

    assert await read_rows("SELECT status, last_error_class FROM jobs WHERE status != 'done'") == [
        {"status": "waiting", "last_error_class": "retry"}
    ]
    assert await sink(read_rows) == {}

    clock.move_to(NOW + timedelta(minutes=10))
    assert await flushed.flush() == 1
    assert keys(flushed) == ["pay_00", "pay_01", "pay_02"]
    assert "Pro plan, renewed" in flushed.payments.values()[3]


# -- tabs -----------------------------------------------------------------------------------------


async def test_a_renamed_payments_tab_keeps_working(flushed: Sheets, read_rows: ReadRows) -> None:
    tab = flushed.payments
    flushed.fake.apply(
        [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": tab.sheet_id, "title": "Razorpay payments"},
                    "fields": "title",
                }
            }
        ]
    )
    await two_more(flushed)

    await flushed.flush()

    renamed = flushed.fake.tab("Razorpay payments")
    assert [row[0] for row in renamed.values()[1:]] == ["pay_00", "pay_01", "pay_02"]
    assert await sink(read_rows) == {}


async def test_a_deleted_sync_status_tab_comes_back(flushed: Sheets, read_rows: ReadRows) -> None:
    flushed.fake.apply([{"deleteSheet": {"sheetId": flushed.fake.tab(SYNC_STATUS).sheet_id}}])
    await two_more(flushed)

    assert await flushed.flush() == 2

    assert await sink(read_rows) == {}
    assert keys(flushed) == ["pay_00", "pay_01", "pay_02"]
    tab = flushed.fake.tab(SYNC_STATUS)
    assert tab.values()[0] == ["Label", "Value"]
    assert status_lines(flushed)["Rows written (last flush)"] == "2"
    stored = await read_rows("SELECT sync_status_tab_id FROM sheet_state")
    assert stored == [{"sync_status_tab_id": tab.sheet_id}]

    await put(flushed.writer, captured("pay_03"))
    assert await flushed.flush() == 1
    assert status_lines(flushed)["Rows written (last flush)"] == "1"


# -- one bad row ----------------------------------------------------------------------------------


async def test_one_bad_cell_among_ten_rows_sends_only_its_job_dead(
    flushed: Sheets, read_rows: ReadRows
) -> None:
    flushed.fake.cell_characters = 1000
    for n in range(1, 11):
        description = "x" * 2000 if n == 4 else "Pro plan"
        await put(flushed.writer, captured(f"pay_{n:02d}", description=description))

    assert await flushed.flush() == 10

    assert keys(flushed) == ["pay_00"] + [f"pay_{n:02d}" for n in range(1, 11) if n != 4]
    jobs = await read_rows(
        "SELECT payment_key, status, last_error_class FROM jobs "
        "WHERE stage = 'mirror' AND status != 'done'"
    )
    assert jobs == [{"payment_key": "pay_04", "status": "dead", "last_error_class": "dead"}]
    assert status_lines(flushed)["Rows written (last flush)"] == "9"
    assert await sink(read_rows) == {}


async def test_a_bad_cell_in_an_update_sends_only_its_job_dead(
    flushed: Sheets, read_rows: ReadRows
) -> None:
    await two_more(flushed)
    await flushed.flush()
    flushed.fake.cell_characters = 1000
    await put(flushed.writer, captured("pay_01", description="y" * 2000))
    await put(flushed.writer, captured("pay_02", description="Pro plan, renewed"))
    await put(flushed.writer, captured("pay_03"))

    await flushed.flush()

    assert keys(flushed) == ["pay_00", "pay_01", "pay_02", "pay_03"]
    assert "Pro plan, renewed" in flushed.payments.values()[3]
    assert await statuses(read_rows) == ["done", "done", "done", "dead", "done", "done"]


# -- first run ------------------------------------------------------------------------------------


async def test_a_first_run_adopts_a_payments_tab_with_our_header_keeping_its_rows(
    sheets: Sheets, read_rows: ReadRows, columns_config: Config
) -> None:
    header = [column.header for column in payment_columns(columns_config)]
    sheets.fake.apply(
        [
            {"addSheet": {"properties": {"sheetId": 77, "title": PAYMENTS}}},
            rows_at(77, [header, ["pay_old"]]),
        ]
    )
    await put(sheets.writer, captured("pay_01"))

    await sheets.flush()

    adopted = sheets.fake.tab(PAYMENTS)
    assert adopted.sheet_id == 77
    assert keys(sheets) == ["pay_old", "pay_01"]
    assert await sink(read_rows) == {}
    assert await read_rows("SELECT payments_tab_id FROM sheet_state") == [{"payments_tab_id": 77}]


async def test_a_first_run_with_another_header_pauses_and_writes_nothing(
    sheets: Sheets, read_rows: ReadRows
) -> None:
    buyer = rows_at(77, [["Id", "Amount"], ["order-1"]])
    sheets.fake.apply([{"addSheet": {"properties": {"sheetId": 77, "title": PAYMENTS}}}, buyer])
    before = [(tab.title, tab.values()) for tab in sheets.fake.tabs]
    await put(sheets.writer, captured("pay_00"))
    await put(sheets.writer, captured("pay_01"))

    await sheets.flush()

    assert "batchUpdate" not in sheets.fake.calls
    assert [(tab.title, tab.values()) for tab in sheets.fake.tabs] == before
    assert await sink(read_rows) == {"state": "paused", "reason": "layout changed"}
    assert len(paused_alerts(sheets)) == 1
    assert [j["attempts"] for j in await waiting_jobs(read_rows)] == [0, 0]
