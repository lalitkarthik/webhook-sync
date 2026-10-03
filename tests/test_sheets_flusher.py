"""The flusher: mirror jobs become rows on the Payments tab (seam: the flusher on the runner).

The flusher runs as a batch stage on the stage runner, against the in-memory fake Sheets
service served through respx (A18). Payments go into the ledger and mirror jobs into the queue;
what comes out is the sheet as the fake holds it, the jobs' states and `sheet_state`. SQLite is
real; time is frozen with time-machine. No call reaches Google.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from fakes.sheets import FakeSheets, Tab
from support import TEST_ACCOUNT, TEST_SPREADSHEET_ID, ReadRows, fixture_service_account
from time_machine import TimeMachineFixture

from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.config import Config
from webhook_sync.db.schema import payments
from webhook_sync.db.writer import Writer
from webhook_sync.queue import Pauses, enqueue
from webhook_sync.queue.runner import HEARTBEAT_EVERY, IDLE_WAIT, BatchStageRunner
from webhook_sync.sinks.sheets import (
    DAILY_SUMMARY,
    FLUSH_EVERY,
    FLUSH_LIMIT,
    HEADER_NOTE,
    PAYMENTS,
    SYNC_STATUS,
    sheets_runner,
)
from webhook_sync.sinks.sheets_client import ServiceAccountTokens, SheetsClient

NOW = datetime(2026, 10, 2, 16, 0, tzinfo=UTC)
CREATED = datetime(2026, 10, 2, 15, 58, 59, tzinfo=UTC)  # 21:28:59 IST


@pytest.fixture(autouse=True)
def frozen_clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(NOW, tick=False)
    return time_machine


async def never_wakes(seconds: float) -> None:
    """The runner's sleep: no heartbeat, no idle wait; the test drives one flush at a time."""
    await asyncio.Event().wait()


@dataclass
class Sheets:
    fake: FakeSheets
    flusher: BatchStageRunner
    writer: Writer
    router: respx.MockRouter

    @property
    def payments(self) -> Tab:
        return self.fake.tab(PAYMENTS)

    async def flush(self) -> int:
        return await self.flusher.run_once()

    def last_write(self) -> list[dict[str, Any]]:
        """The requests of the last `batchUpdate` sent."""
        requests: list[httpx.Request] = [call.request for call in self.router.calls]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        sent = [r for r in requests if r.url.path.endswith(":batchUpdate")]
        return json.loads(sent[-1].content)["requests"]


@pytest.fixture
def sheet_config(config: Config) -> Config:
    """The test config with one metadata key, `order_id`."""
    return replace(config, razorpay=replace(config.razorpay, metadata_keys=("order_id",)))


@pytest.fixture
async def sheets(writer: Writer, sheet_config: Config) -> AsyncIterator[Sheets]:
    """The flusher on the runner, writing to a fresh fake spreadsheet."""
    fake = FakeSheets(TEST_SPREADSHEET_ID)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        async with httpx.AsyncClient() as http:
            client = SheetsClient(
                http, TEST_SPREADSHEET_ID, ServiceAccountTokens(fixture_service_account())
            )
            flusher = sheets_runner(
                writer,
                client,
                sheet_config,
                claimed_by="wsync-test",
                pauses=Pauses(),
                sleep=never_wakes,
            )
            yield Sheets(fake, flusher, writer, router)


def captured(payment_id: str = "pay_A", **changes: Any) -> dict[str, Any]:
    """A captured INR payment as the ledger holds it."""
    row: dict[str, Any] = {
        "source": "razorpay",
        "account": TEST_ACCOUNT,
        "payment_id": payment_id,
        "status": "succeeded",
        "source_status": "captured",
        "amount_minor": 50000,
        "currency": "INR",
        "refunded_minor": 0,
        "fee_minor": 1180,
        "fee_tax_minor": 180,
        "net_minor": 48820,
        "settlement_currency": "INR",
        "method": "netbanking (CNRB)",
        "metadata": '{"order_id": "A-1042"}',
        "customer_email": None,
        "description": "Pro plan",
        "source_created_at": to_db_text(CREATED),
        "via": "delivery",
        "row_hash": "fixture",
        "row_version": 1,
        "updated_at": to_db_text(NOW),
    }
    return row | changes


async def put(writer: Writer, row: dict[str, Any]) -> int:
    """Write the payment's ledger row and enqueue its mirror job, as the ledger worker does."""
    async with writer.transaction() as tx:
        await tx.execute(payments.delete().where(payments.c.payment_id == row["payment_id"]))
        await tx.execute(payments.insert().values(row))
        return await enqueue(
            tx,
            stage="mirror",
            sink="sheets",
            source=row["source"],
            account=row["account"],
            payment_key=row["payment_id"],
            event_id=None,
            retry_profile="production",
            now=utc_now(),
        )


def shown(tab: Tab, row: int) -> list[str]:
    """A row's cells as the sheet shows them."""
    return [
        tab.show(row, column, formatted=True).get("formattedValue", "")
        for column in range(len(tab.values()[0]))
    ]


async def statuses(read_rows: ReadRows) -> list[str]:
    rows = await read_rows("SELECT status FROM jobs WHERE stage = 'mirror' ORDER BY id")
    return [row["status"] for row in rows]


async def test_a_captured_payment_reaches_a_fresh_sheet_as_one_typed_row(
    sheets: Sheets, read_rows: ReadRows
) -> None:
    await put(sheets.writer, captured())

    assert await sheets.flush() == 1

    assert [tab.title for tab in sheets.fake.tabs] == [
        "Sheet1",
        PAYMENTS,
        DAILY_SUMMARY,
        SYNC_STATUS,
    ]
    tab = sheets.payments
    assert tab.values()[0] == [
        "Payment ID",
        "Source",
        "Created (IST)",
        "Status",
        "Amount",
        "Currency",
        "Refunded",
        "Fee",
        "GST on fee",
        "Net",
        "Fee currency",
        "Method",
        "Customer email",
        "Description",
        "order_id",
        "Via",
        "Note",
        "Updated (IST)",
    ]
    assert shown(tab, 1) == [
        "pay_A",
        "razorpay",
        "2026-10-02 21:28:59",
        "succeeded",
        "500.00",
        "INR",
        "0.00",
        "11.80",
        "1.80",
        "488.20",
        "INR",
        "netbanking (CNRB)",
        "",
        "Pro plan",
        "A-1042",
        "delivery",
        "",
        "2026-10-02 21:30:00",
    ]
    assert tab.value(1, 4) == 500.0  # a number, not text
    assert len(tab.values()) == 2
    assert await statuses(read_rows) == ["done"]


async def test_a_fresh_sheet_gets_three_tabs_with_header_note_and_frozen_header_and_key(
    sheets: Sheets, read_rows: ReadRows
) -> None:
    await put(sheets.writer, captured())

    await sheets.flush()

    for title in (PAYMENTS, DAILY_SUMMARY, SYNC_STATUS):
        tab = sheets.fake.tab(title)
        assert tab.cell(0, 0)["note"] == HEADER_NOTE
        assert tab.properties["gridProperties"]["frozenRowCount"] == 1
        assert tab.properties["gridProperties"]["frozenColumnCount"] >= 1
    assert sheets.fake.tab(DAILY_SUMMARY).values()[0] == [
        "Day",
        "Currency",
        "Fee currency",
        "Payments",
        "Gross",
        "Refunds",
        "Fees",
        "Net (before refunds)",
        "Fee pending",
    ]
    [state] = await read_rows("SELECT * FROM sheet_state")
    assert state["spreadsheet_id"] == TEST_SPREADSHEET_ID
    assert state["payments_tab_id"] == sheets.payments.sheet_id
    assert state["daily_summary_tab_id"] == sheets.fake.tab(DAILY_SUMMARY).sheet_id
    assert state["sync_status_tab_id"] == sheets.fake.tab(SYNC_STATUS).sheet_id
    assert state["last_flush_at"] == to_db_text(NOW)
    assert (state["rows_written_last"], state["rows_written_total"]) == (1, 1)


async def test_a_payment_that_changes_is_updated_in_place_its_key_cell_untouched(
    sheets: Sheets, read_rows: ReadRows
) -> None:
    await put(sheets.writer, captured())
    await sheets.flush()
    await put(sheets.writer, captured(status="refunded", refunded_minor=50000))
    await sheets.flush()

    tab = sheets.payments
    assert len(tab.values()) == 2
    assert tab.value(1, 0) == "pay_A"
    [update] = sheets.last_write()
    assert update["updateCells"]["start"]["rowIndex"] == 1
    assert update["updateCells"]["start"]["columnIndex"] == 1  # the key cell never rewritten
    assert shown(tab, 1)[3:7] == ["refunded", "500.00", "INR", "500.00"]
    assert await statuses(read_rows) == ["done", "done"]
    [state] = await read_rows("SELECT rows_written_last, rows_written_total FROM sheet_state")
    assert state == {"rows_written_last": 1, "rows_written_total": 2}


async def test_fee_columns_say_pending_while_the_fee_is_unknown(sheets: Sheets) -> None:
    await put(sheets.writer, captured(fee_minor=None, fee_tax_minor=None, net_minor=None))

    await sheets.flush()

    assert shown(sheets.payments, 1)[7:10] == ["pending", "pending", "pending"]


async def test_ten_payments_cost_one_read_and_one_write_appended_in_order_after_the_last(
    sheets: Sheets, read_rows: ReadRows
) -> None:
    await put(sheets.writer, captured("pay_00"))
    await sheets.flush()
    sheets.fake.calls.clear()
    for n in range(1, 11):
        await put(sheets.writer, captured(f"pay_{n:02d}"))

    assert await sheets.flush() == 10

    assert sheets.fake.calls == ["getByDataFilter", "batchUpdate"]
    keys = [row[0] for row in sheets.payments.values()[1:]]
    assert keys == [f"pay_{n:02d}" for n in range(11)]
    assert set(await statuses(read_rows)) == {"done"}


async def test_appends_grow_the_grid_when_it_is_too_small(sheets: Sheets) -> None:
    await put(sheets.writer, captured("pay_00"))
    await sheets.flush()
    tab = sheets.payments
    sheets.fake.apply(
        [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": tab.sheet_id, "gridProperties": {"rowCount": 3}},
                    "fields": "gridProperties.rowCount",
                }
            }
        ]
    )
    for n in range(1, 6):
        await put(sheets.writer, captured(f"pay_{n:02d}"))

    await sheets.flush()

    tab = sheets.payments
    assert [row[0] for row in tab.values()[1:]] == [f"pay_{n:02d}" for n in range(6)]
    assert tab.row_count == 7


async def test_a_read_failure_writes_nothing_and_retries_every_job(
    sheets: Sheets, read_rows: ReadRows
) -> None:
    await put(sheets.writer, captured("pay_00"))
    await sheets.flush()
    sheets.fake.calls.clear()
    await put(sheets.writer, captured("pay_01"))
    await put(sheets.writer, captured("pay_02"))
    sheets.fake.fail_next(503)

    await sheets.flush()

    assert "batchUpdate" not in sheets.fake.calls
    assert len(sheets.payments.values()) == 2
    jobs = await read_rows(
        "SELECT status, last_error_class FROM jobs WHERE stage = 'mirror' ORDER BY id"
    )
    assert jobs[1:] == [
        {"status": "waiting", "last_error_class": "retry"},
        {"status": "waiting", "last_error_class": "retry"},
    ]


async def test_a_lost_write_reply_is_retried_and_the_next_flush_updates_the_same_rows(
    sheets: Sheets, read_rows: ReadRows, frozen_clock: TimeMachineFixture
) -> None:
    await put(sheets.writer, captured("pay_00"))
    await sheets.flush()
    await put(sheets.writer, captured("pay_01"))
    await put(sheets.writer, captured("pay_02"))
    sheets.fake.lose_next_write_reply()

    await sheets.flush()

    assert [row[0] for row in sheets.payments.values()[1:]] == ["pay_00", "pay_01", "pay_02"]
    assert await statuses(read_rows) == ["done", "waiting", "waiting"]

    frozen_clock.move_to(utc_now() + FLUSH_EVERY * 100)
    assert await sheets.flush() == 2

    assert [row[0] for row in sheets.payments.values()[1:]] == ["pay_00", "pay_01", "pay_02"]
    assert await statuses(read_rows) == ["done", "done", "done"]


async def test_a_payment_with_no_ledger_row_completes_without_a_row(
    sheets: Sheets, read_rows: ReadRows
) -> None:
    await put(sheets.writer, captured("pay_00"))
    async with sheets.writer.transaction() as tx:
        await tx.execute(payments.delete())

    await sheets.flush()

    assert len(sheets.payments.values()) == 1
    assert await statuses(read_rows) == ["done"]


async def test_the_flusher_flushes_every_3_s_while_jobs_wait_and_at_once_when_200_do(
    writer: Writer, config: Config
) -> None:
    rows_seen: list[int] = []
    flusher: BatchStageRunner | None = None
    fake = FakeSheets(TEST_SPREADSHEET_ID)

    async def sleep(seconds: float) -> None:
        if seconds == HEARTBEAT_EVERY.total_seconds():
            await asyncio.Event().wait()  # no heartbeats in this test
        assert seconds == IDLE_WAIT.total_seconds()
        rows_seen.append(len(fake.tab(PAYMENTS).values()) - 1)
        if len(rows_seen) == 1:  # 200 arrive during the first wait
            for n in range(1, FLUSH_LIMIT + 1):
                await put(writer, captured(f"pay_{n:03d}"))
        if len(rows_seen) == 2:  # one more arrives after that flush
            await put(writer, captured("pay_999"))
        if len(rows_seen) == 6 and flusher is not None:
            flusher.stop()

    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        async with httpx.AsyncClient() as http:
            client = SheetsClient(
                http, TEST_SPREADSHEET_ID, ServiceAccountTokens(fixture_service_account())
            )
            flusher = sheets_runner(
                writer, client, config, claimed_by="t", pauses=Pauses(), sleep=sleep
            )
            await put(writer, captured("pay_000"))
            await asyncio.wait_for(flusher.run(), 10)

    # One wait, then 200 at once; then 3 s of waits before the one left waiting is flushed.
    assert rows_seen == [1, 201, 201, 201, 202, 202]
    assert FLUSH_EVERY == 3 * IDLE_WAIT
