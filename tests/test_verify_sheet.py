"""Verify, ledger against sheet: every hour the sheet is checked and repaired (#41).

Seam: the running app. Its lifespan runs the hourly check on the hour in `service.timezone`,
beside the ledger stage and the flusher; Google is P3's fake Sheets and Razorpay's lists answer
empty. Each test runs the app twice: once, away from the hour, so the flusher writes the
ledger's payments to the sheet; then, after a person's edits, just before the hour, so exactly
one check runs. What comes out is what an outsider sees: `verify_runs`, `verify_findings`,
jobs, the sheet and its Sync status. SQLite is real; our clock is moved with time-machine and
keeps ticking.
"""

import asyncio
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fakes.sheets import FakeSheets, Tab
from support import TEST_ACCOUNT, TEST_SPREADSHEET_ID, ReadRows
from time_machine import TimeMachineFixture

from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.config import Config
from webhook_sync.db.schema import payments
from webhook_sync.db.writer import Writer
from webhook_sync.queue import enqueue
from webhook_sync.queue import runner as runner_module
from webhook_sync.receiver.app import create_app
from webhook_sync.sinks import sheets as sheets_module
from webhook_sync.sinks.sheets import PAYMENTS, SYNC_STATUS

RAZORPAY_API = "https://api.razorpay.test"
FLUSHED_AT = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)  # 20:30 IST: half an hour from the hour
CHECKED_AT = datetime(2026, 10, 2, 15, 30, tzinfo=UTC)  # 21:00 IST: on the hour
JUST_BEFORE = CHECKED_AT - timedelta(seconds=1)
CREATED = datetime(2026, 10, 2, 9, 58, 59, tzinfo=UTC)  # 15:28:59 IST, hours before


@pytest.fixture(autouse=True)
def clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(FLUSHED_AT, tick=True)
    return time_machine


@pytest.fixture(autouse=True)
def quick_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stage runners' idle waits short, so a flush follows a job at once."""
    monkeypatch.setattr(runner_module, "IDLE_WAIT", timedelta(milliseconds=10))
    monkeypatch.setattr(sheets_module, "FLUSH_EVERY", timedelta(milliseconds=30))


@pytest.fixture
def checked_config(config: Config) -> Config:
    return replace(config, razorpay=replace(config.razorpay, api_url=RAZORPAY_API))


@dataclass
class Outside:
    sheets: FakeSheets
    router: respx.MockRouter

    @property
    def payments(self) -> Tab:
        return self.sheets.tab(PAYMENTS)

    def verify_reads(self) -> int:
        """The `getByDataFilter` calls that read cell values, as only verify does."""
        calls: list[Any] = list(self.router.calls)  # pyright: ignore[reportUnknownArgumentType]
        return sum(
            1
            for call in calls
            if call.request.url.path.endswith(":getByDataFilter")
            and "userEnteredValue" in call.request.url.params.get("fields", "")
        )


@pytest.fixture
def outside() -> Iterator[Outside]:
    """Google faked by P3's fake Sheets; Razorpay's lists answer empty, so the sweep is quiet."""
    sheets = FakeSheets(TEST_SPREADSHEET_ID)
    with respx.mock(assert_all_called=False) as router:
        sheets.mount(router)
        for path in ("/v1/payments", "/v1/refunds"):
            router.get(f"{RAZORPAY_API}{path}").mock(side_effect=empty_list)
        yield Outside(sheets, router)


def empty_list(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"entity": "collection", "count": 0, "items": []},
        headers={"date": format_datetime(utc_now(), usegmt=True)},
    )


@asynccontextmanager
async def running(config: Config, data_dir: Path) -> AsyncGenerator[None]:
    app = create_app(config, data_dir)
    async with app.router.lifespan_context(app):
        yield


async def wait_for(read_rows: ReadRows, sql: str, expected: list[dict[str, Any]]) -> None:
    """Wait (in real time) until `sql` reads `expected`: the app's loops work in the
    background."""
    for _ in range(500):
        if await read_rows(sql) == expected:
            return
        await asyncio.sleep(0.01)
    assert await read_rows(sql) == expected


def captured(payment_id: str, **changes: Any) -> dict[str, Any]:
    """A captured INR payment, its fee known, as the ledger holds it."""
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
        "customer_email": "a.b@gmail.com",
        "description": "Pro plan",
        "source_created_at": to_db_text(CREATED),
        "via": "delivery",
        "row_hash": "fixture",
        "row_version": 1,
        "updated_at": to_db_text(FLUSHED_AT - timedelta(hours=1)),
    }
    return row | changes


async def put(writer: Writer, *rows: dict[str, Any]) -> None:
    """Write each payment's ledger row with its mirror job, as the ledger worker does."""
    async with writer.transaction() as tx:
        for row in rows:
            await tx.execute(payments.insert().values(row))
            await enqueue(
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


MIRRORED = "SELECT count(*) AS n FROM jobs WHERE stage = 'mirror' AND status = 'done'"
RUN = (
    "SELECT kind, payments_checked, findings_count, repaired_count, status_line"
    " FROM verify_runs WHERE finished_at IS NOT NULL"
)
FINDINGS = (
    "SELECT kind, payment_id, repair_job_id IS NOT NULL AS repaired FROM verify_findings"
    " ORDER BY kind, payment_id"
)


async def flushed(
    config: Config, data_dir: Path, writer: Writer, read_rows: ReadRows, *rows: dict[str, Any]
) -> None:
    """The payments in the ledger and on the sheet, the app stopped again."""
    await put(writer, *rows)
    async with running(config, data_dir):
        await wait_for(read_rows, MIRRORED, [{"n": len(rows)}])


@asynccontextmanager
async def checked(
    config: Config, data_dir: Path, read_rows: ReadRows, clock: TimeMachineFixture
) -> AsyncGenerator[None]:
    """The app started a second before the hour, held until its check has run."""
    clock.move_to(JUST_BEFORE, tick=True)
    async with running(config, data_dir):
        await wait_for(
            read_rows,
            "SELECT count(*) AS n FROM verify_runs WHERE finished_at IS NOT NULL",
            [{"n": 1}],
        )
        yield


def last_verify(sheets: FakeSheets) -> str | None:
    rows = dict([*row, None][:2] for row in sheets.tab(SYNC_STATUS).values()[1:])
    return rows.get("Last verify")


async def test_a_clean_sheet_gives_0_differences_with_exactly_one_read(
    checked_config: Config,
    data_dir: Path,
    writer: Writer,
    outside: Outside,
    read_rows: ReadRows,
    clock: TimeMachineFixture,
) -> None:
    await flushed(checked_config, data_dir, writer, read_rows, captured("pay_A"), captured("pay_B"))
    reads_before = outside.verify_reads()

    async with checked(checked_config, data_dir, read_rows, clock):
        line = "Sheet checked 02 Oct 21:00 IST: 2 payments, 0 differences"
        for _ in range(500):
            if last_verify(outside.sheets) == line:
                break
            await asyncio.sleep(0.01)

    assert reads_before == 0
    assert outside.verify_reads() == 1
    assert await read_rows(RUN) == [
        {
            "kind": "quick",
            "payments_checked": 2,
            "findings_count": 0,
            "repaired_count": 0,
            "status_line": line,
        }
    ]
    assert await read_rows(FINDINGS) == []
    # Shown on Sync status by a flush of its own: no mirror job was made for it.
    assert last_verify(outside.sheets) == line
    assert await read_rows("SELECT count(*) AS n FROM jobs WHERE stage = 'mirror'") == [{"n": 2}]
