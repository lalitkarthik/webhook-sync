"""The flusher: mirror jobs become rows on the Payments tab (seam: the flusher on the runner).

The flusher runs as a batch stage on the stage runner, against the in-memory fake Sheets
service served through respx (A18). Payments go into the ledger and mirror jobs into the queue;
what comes out is the sheet as the fake holds it, the jobs' states and `sheet_state`. SQLite is
real; time is frozen with time-machine. No call reaches Google.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from fakes.sheets import FakeSheets, Tab
from support import TEST_ACCOUNT, TEST_SPREADSHEET_ID, ReadRows, fixture_service_account
from time_machine import TimeMachineFixture

from webhook_sync.alerts import Alerter
from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.config import Config
from webhook_sync.db.schema import payments
from webhook_sync.db.writer import Writer
from webhook_sync.queue import Pauses, enqueue
from webhook_sync.queue.pause import record_pause, register
from webhook_sync.queue.runner import HEARTBEAT_EVERY, IDLE_WAIT, BatchStageRunner
from webhook_sync.receiver.app import create_app
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

HOOK = "https://hooks.example.test/alerts"
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
    hook: respx.Route

    def alerts(self) -> list[str]:
        """The texts of the alerts posted."""
        calls: list[Any] = list(self.hook.calls)  # pyright: ignore[reportUnknownArgumentType]
        return [json.loads(call.request.content)["text"] for call in calls]

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
    """The flusher on the runner, writing to a fresh fake spreadsheet, alerting to `HOOK`."""
    async with flusher_on(writer, sheet_config) as sheets:
        yield sheets


@asynccontextmanager
async def flusher_on(writer: Writer, config: Config) -> AsyncGenerator[Sheets]:
    fake = FakeSheets(TEST_SPREADSHEET_ID)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        hook = router.post(HOOK).respond(200, text="ok")
        alerter = Alerter(writer, HOOK)
        async with httpx.AsyncClient() as http:
            client = SheetsClient(
                http, TEST_SPREADSHEET_ID, ServiceAccountTokens(fixture_service_account())
            )
            flusher = sheets_runner(
                writer,
                client,
                config,
                claimed_by="wsync-test",
                pauses=Pauses(),
                sleep=never_wakes,
                alerter=alerter,
            )
            try:
                yield Sheets(fake, flusher, writer, router, hook)
            finally:
                await alerter.aclose()


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
        "customer_email": "a.b@gmail.com",
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
        "a***@gmail.com",
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
    [update] = [
        r for r in sheets.last_write() if r["updateCells"]["start"]["sheetId"] == tab.sheet_id
    ]
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


async def test_the_server_runs_the_flusher_in_its_lifespan(
    writer: Writer, sheet_config: Config, read_rows: ReadRows
) -> None:
    fake = FakeSheets(TEST_SPREADSHEET_ID)
    await put(writer, captured())
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        app = create_app(sheet_config, sheet_config.service.data_dir)
        async with app.router.lifespan_context(app):
            for _ in range(500):
                if await statuses(read_rows) == ["done"]:
                    break
                await asyncio.sleep(0.01)
    assert await statuses(read_rows) == ["done"]
    assert fake.tab(PAYMENTS).values()[1][0] == "pay_A"


# -- exact values (#32) ----------------------------------------------------------------------


async def test_a_custom_column_list_writes_exactly_those_headers_in_that_order(
    writer: Writer, sheet_config: Config
) -> None:
    config = replace(
        sheet_config,
        sheets=replace(sheet_config.sheets, columns=("payment_id", "metadata", "amount")),
        razorpay=replace(sheet_config.razorpay, metadata_keys=("order_id", "plan")),
    )
    async with flusher_on(writer, config) as sheets:
        await put(writer, captured(metadata='{"order_id": "A-1042", "plan": "pro"}'))

        await sheets.flush()

        assert sheets.payments.values() == [
            ["Payment ID", "order_id", "plan", "Amount"],
            ["pay_A", "A-1042", "pro", 500.0],
        ]


async def test_amounts_take_their_currencys_decimals(sheets: Sheets) -> None:
    await put(sheets.writer, captured("pay_JPY", amount_minor=1999, currency="JPY"))
    await put(sheets.writer, captured("pay_USD", amount_minor=1999, currency="usd"))

    await sheets.flush()

    rows = {row[0]: row for row in sheets.payments.values()[1:]}
    assert rows["pay_JPY"][4] == 1999
    assert rows["pay_USD"][4] == 19.99
    assert shown(sheets.payments, 1)[4] == "1999"
    assert shown(sheets.payments, 2)[4] == "19.99"


async def test_an_unknown_currency_holds_back_only_that_row_dead_with_one_alert(
    sheets: Sheets, read_rows: ReadRows
) -> None:
    await put(sheets.writer, captured("pay_00"))
    await put(sheets.writer, captured("pay_01", currency="XYZ"))
    await put(sheets.writer, captured("pay_02"))

    assert await sheets.flush() == 3

    assert [row[0] for row in sheets.payments.values()[1:]] == ["pay_00", "pay_02"]
    jobs = await read_rows(
        "SELECT payment_key, status, last_error_class, last_error FROM jobs "
        "WHERE stage = 'mirror' ORDER BY id"
    )
    assert [(j["payment_key"], j["status"]) for j in jobs] == [
        ("pay_00", "done"),
        ("pay_01", "dead"),
        ("pay_02", "done"),
    ]
    assert "XYZ" in jobs[1]["last_error"]
    [alert] = sheets.alerts()
    assert "unknown_currency" in alert
    assert "XYZ" in alert
    assert "pay_01" in alert
    [state] = await read_rows("SELECT rows_written_last FROM sheet_state")
    assert state["rows_written_last"] == 2


async def test_a_60000_character_description_with_emoji_is_cut_and_accepted(
    sheets: Sheets, read_rows: ReadRows
) -> None:
    await put(sheets.writer, captured(description="😀 " + "x" * 59997))

    await sheets.flush()

    cell: str = sheets.payments.value(1, 13)
    assert len(cell.encode("utf-16-le")) // 2 == 50000
    assert cell.startswith("😀 x")
    assert cell.endswith("…[truncated]")
    assert await statuses(read_rows) == ["done"]


async def test_emails_are_written_as_is_when_masking_is_off(
    writer: Writer, sheet_config: Config
) -> None:
    config = replace(sheet_config, sheets=replace(sheet_config.sheets, mask_email=False))
    async with flusher_on(writer, config) as sheets:
        await put(writer, captured("pay_A"))
        await put(writer, captured("pay_B", customer_email="x"))

        await sheets.flush()

        assert [row[12] for row in sheets.payments.values()[1:]] == ["a.b@gmail.com", "x"]


async def test_an_email_without_an_at_is_masked_whole(sheets: Sheets) -> None:
    await put(sheets.writer, captured(customer_email="x"))

    await sheets.flush()

    assert sheets.payments.value(1, 12) == "***"


async def test_a_row_not_found_at_source_with_unknown_columns_gets_both_notes(
    sheets: Sheets,
) -> None:
    await put(
        sheets.writer,
        captured(not_found_at=to_db_text(NOW), unknown_columns='["fee", "gst_on_fee"]'),
    )

    await sheets.flush()

    assert sheets.payments.value(1, 16) == "not found at source; unknown: fee, gst_on_fee"


async def test_formula_phone_and_zero_padded_text_stay_text(sheets: Sheets) -> None:
    await put(
        sheets.writer,
        captured(
            description='=HYPERLINK("http://x")',
            method="+919876543210",
            metadata='{"order_id": "007"}',
        ),
    )

    await sheets.flush()

    row = sheets.payments.values()[1]
    assert (row[11], row[13], row[14]) == ("+919876543210", '=HYPERLINK("http://x")', "007")


# -- Daily summary and Sync status (#33) ------------------------------------------------------


def summary(sheets: Sheets) -> list[list[str]]:
    """The Daily summary's rows below the header, as the sheet shows them."""
    tab = sheets.fake.tab(DAILY_SUMMARY)
    return [shown(tab, row) for row in range(1, len(tab.values()))]


def status_lines(sheets: Sheets) -> dict[str, str]:
    """Sync status as label → shown value."""
    tab = sheets.fake.tab(SYNC_STATUS)
    lines: dict[str, str] = {}
    for row in range(1, tab.last_used_row() + 1):
        label = tab.value(row, 0)
        lines[label] = tab.show(row, 1, formatted=True).get("formattedValue", "")
    return lines


async def test_three_payments_on_two_ist_days_make_two_summary_rows(sheets: Sheets) -> None:
    # 00:10 IST on 2 Oct is still 1 Oct in UTC: it counts on 2 Oct.
    after_midnight = datetime(2026, 10, 1, 18, 40, tzinfo=UTC)
    await put(sheets.writer, captured("pay_1", source_created_at=to_db_text(after_midnight)))
    await put(
        sheets.writer,
        captured(
            "pay_2",
            source_created_at=to_db_text(datetime(2026, 10, 2, 10, 0, tzinfo=UTC)),
            amount_minor=20000,
            refunded_minor=5000,
            fee_minor=472,
            fee_tax_minor=72,
            net_minor=19528,
        ),
    )
    await put(
        sheets.writer,
        captured("pay_3", source_created_at=to_db_text(datetime(2026, 10, 1, 10, 0, tzinfo=UTC))),
    )

    await sheets.flush()

    assert sorted(summary(sheets)) == [
        ["2026-10-01", "INR", "INR", "1", "500.00", "0.00", "11.80", "488.20", "0"],
        ["2026-10-02", "INR", "INR", "2", "700.00", "50.00", "16.52", "683.48", "0"],
    ]


async def test_a_fee_pending_payment_counts_in_gross_not_fees_and_is_corrected_later(
    sheets: Sheets,
) -> None:
    await put(sheets.writer, captured(fee_minor=None, fee_tax_minor=None, net_minor=None))
    await sheets.flush()

    assert summary(sheets) == [
        ["2026-10-02", "INR", "INR", "1", "500.00", "0.00", "0.00", "0.00", "1"]
    ]

    await put(sheets.writer, captured())
    await sheets.flush()

    assert summary(sheets) == [
        ["2026-10-02", "INR", "INR", "1", "500.00", "0.00", "11.80", "488.20", "0"]
    ]


async def test_a_late_payment_for_an_older_day_updates_that_row_in_place(
    sheets: Sheets,
) -> None:
    older = to_db_text(datetime(2026, 9, 30, 6, 0, tzinfo=UTC))
    await put(sheets.writer, captured("pay_old", source_created_at=older))
    await sheets.flush()
    await put(sheets.writer, captured("pay_new"))
    await sheets.flush()
    sheets.fake.calls.clear()

    await put(sheets.writer, captured("pay_late", source_created_at=older))
    await sheets.flush()

    assert summary(sheets) == [
        ["2026-09-30", "INR", "INR", "2", "1000.00", "0.00", "23.60", "976.40", "0"],
        ["2026-10-02", "INR", "INR", "1", "500.00", "0.00", "11.80", "488.20", "0"],
    ]
    assert sheets.fake.calls == ["getByDataFilter", "batchUpdate"]
    tab_id = sheets.fake.tab(DAILY_SUMMARY).sheet_id
    summary_writes = [
        r["updateCells"]["start"]
        for r in sheets.last_write()
        if "updateCells" in r and r["updateCells"]["start"]["sheetId"] == tab_id
    ]
    assert summary_writes == [{"sheetId": tab_id, "rowIndex": 1, "columnIndex": 3}]


async def test_a_failed_payment_changes_no_summary_row(sheets: Sheets) -> None:
    await put(sheets.writer, captured("pay_ok"))
    await sheets.flush()
    before = summary(sheets)

    await put(sheets.writer, captured("pay_failed", status="failed", source_status="failed"))
    await put(
        sheets.writer,
        captured(
            "pay_failed_old",
            status="failed",
            source_status="failed",
            source_created_at=to_db_text(datetime(2026, 9, 1, tzinfo=UTC)),
        ),
    )
    await sheets.flush()

    assert summary(sheets) == before
    assert len(sheets.payments.values()) == 4


async def test_sync_status_shows_the_flush_numbers_and_both_states(
    sheets: Sheets, read_rows: ReadRows
) -> None:
    await put(sheets.writer, captured("pay_00"))
    await sheets.flush()
    await put(sheets.writer, captured("pay_01"))
    await put(sheets.writer, captured("pay_02", currency="XYZ"))
    await put(sheets.writer, captured("pay_03"))

    await sheets.flush()

    tab = sheets.fake.tab(SYNC_STATUS)
    assert tab.values()[0] == ["Label", "Value"]
    [state] = await read_rows("SELECT deployment_id FROM sheet_state")
    assert state["deployment_id"]
    assert status_lines(sheets) == {
        "Last flush": "2026-10-02 21:30:00",
        "Rows written (last flush)": "2",
        "Rows written (total)": "3",
        "Sheet jobs waiting": "0",
        "Dead jobs": "1",
        "Sheet state": "active",
        "Razorpay state": "active",
        "Last verify": "",
        "Deployment id": state["deployment_id"],
    }


async def test_sync_status_counts_jobs_still_waiting_and_shows_a_paused_source(
    writer: Writer, sheet_config: Config
) -> None:
    async with flusher_on(writer, sheet_config) as sheets:
        await register(writer, {"razorpay": "source", "sheets": "sink"})
        async with writer.transaction() as tx:
            await record_pause(tx, "source", "razorpay", "credentials rejected", now=NOW)
        for n in range(FLUSH_LIMIT + 3):
            await put(writer, captured(f"pay_{n:03d}"))

        await sheets.flush()

        lines = status_lines(sheets)
        assert lines["Sheet jobs waiting"] == "3"
        assert lines["Razorpay state"] == "paused: credentials rejected"
        assert lines["Sheet state"] == "active"
