"""Verify, ledger against sheet (verify.md, Q150): the quick run, on the hour.

One `getByDataFilter` reads the cell values of the Payments and Daily summary tabs by their
stored tab ids. Each ledger payment is encoded exactly as the flusher would write it and compared
cell by cell over the configured columns; each Daily summary row is recomputed from the ledger
and compared the same way. What disagrees is a finding:

- `sheet_missing`: a payment with no Payments row; `sheet_differs`: its row (the first, when
  there are two) differs in a column; both repaired by a mirror job;
- `sheet_duplicate`: a payment on more than one row; `sheet_foreign`: a row whose key is no
  payment in the ledger; both reported only, never deleted;
- `summary_differs`: a Daily summary row missing or different, repaired by a mirror job for one
  payment of its day (the flusher recomputes every day it touches).

Work in progress is skipped, never reported: a payment with a live job, changed since the run
started, or under an hour old with the fee pending (Q97), and the summary days they fall on.

Each run is stored in `verify_runs` with its findings in `verify_findings`, the repair jobs in
the same transaction, and its line (`Sheet checked 02 Oct 21:00 IST: 7 payments, 0
differences`) for Sync status, which a status-only flush then writes (A17). Nothing here logs
the spreadsheet id or an email.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.config import Config
from webhook_sync.db.schema import jobs, payments, sheet_state, verify_findings, verify_runs
from webhook_sync.db.writer import Writer
from webhook_sync.queue import Pauses, enqueue
from webhook_sync.queue.operations import LIVE_STATUSES
from webhook_sync.sinks.encoding import UnknownCurrency, payment_columns, zone_label
from webhook_sync.sinks.sheets import DAILY_SUMMARY_HEADER, RAZORPAY, SINK, STAGE
from webhook_sync.sinks.sheets_client import Json, SheetsClient, SheetsFailure
from webhook_sync.sinks.summary import (
    SummaryKey,
    Totals,
    day_of,
    read_key,
    summarise,
    summary_cells,
)

log = logging.getLogger("webhook_sync.verify")

type FindingKind = Literal[
    "sheet_missing", "sheet_differs", "sheet_duplicate", "sheet_foreign", "summary_differs"
]

CHECK_EVERY = timedelta(hours=1)
"""The quick run's period, on its multiples from local midnight: on the hour."""

FEE_SETTLES = timedelta(hours=1)
"""A payment younger than this whose fee is pending is skipped: the fee is on its way (Q97)."""

VALUES_FIELDS = (
    "sheets(properties(sheetId),data(startRow,rowData(values("
    "userEnteredValue,effectiveValue,userEnteredFormat.numberFormat))))"
)

type Sleep = Callable[[float], Awaitable[None]]
type PaymentKey = tuple[str, str, str]
type Rows = list[tuple[int, list[Json]]]
"""A tab's rows below the header, as read: each row's index and cells."""


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    payment_id: str | None
    detail: str | None = None
    repair: str | None = None
    """The payment whose mirror job repairs it, when one can."""


class SheetVerify:
    """The quick run for the configured spreadsheet: check, record, repair, show the line."""

    def __init__(
        self,
        writer: Writer,
        client: SheetsClient,
        config: Config,
        *,
        pauses: Pauses,
        request_flush: Callable[[], None],
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._writer = writer
        self._client = client
        self._config = config
        self._pauses = pauses
        self._request_flush = request_flush
        self._sleep = sleep
        self._zone = config.service.timezone
        self._columns = payment_columns(config)
        self._key = next(i for i, c in enumerate(self._columns) if c.key == "payment_id")

    async def run_hourly(self) -> None:
        """A quick run on the hour in `service.timezone`, until cancelled. A run that raises is
        logged; the next one runs on time."""
        while True:
            await self._sleep(until_next(utc_now(), self._zone, CHECK_EVERY))
            try:
                await self.run()
            except Exception:
                log.exception("verify run failed; the next one runs on time")

    async def run(self) -> int | None:
        """One quick run; its `verify_runs` id, or None when there was nothing to check (the
        sheet paused, or never written) or the read failed."""
        started = utc_now()
        clock = time.perf_counter()
        if SINK in self._pauses.sinks:
            log.info("verify skipped: the sheet is paused")
            return None
        tabs = await self._tabs()
        if tabs is None:
            log.info("verify skipped: nothing written to the sheet yet")
            return None
        answer = await self._client.get_by_data_filter(
            [
                _rows(tabs[0], len(self._columns)),
                _rows(tabs[1], len(DAILY_SUMMARY_HEADER)),
            ],
            fields=VALUES_FIELDS,
        )
        if isinstance(answer, SheetsFailure):
            log.warning(
                "verify read failed",
                extra={"error": answer.error, "error_class": answer.error_class},
            )
            return None
        read = _by_tab(answer)
        ledger, live = await self._ledger()
        skipped = {
            key for key, row in ledger.items() if key in live or self._in_progress(row, started)
        }
        checked, findings = self._payments(ledger, skipped, read.get(tabs[0], []))
        findings += self._summary(ledger, skipped, read.get(tabs[1], []))
        run_id, repaired = await self._record(started, checked, findings)
        self._request_flush()
        log.info(
            "verify run",
            extra={
                "kind": "quick",
                "payments_checked": checked,
                "findings": len(findings),
                "repaired": repaired,
                "duration_ms": round((time.perf_counter() - clock) * 1000),
            },
        )
        return run_id

    # -- what is compared ---------------------------------------------------------------------

    def _in_progress(self, row: Mapping[str, Any], started: datetime) -> bool:
        """Changed since the run started, or under an hour old with the fee pending."""
        if row["updated_at"] >= to_db_text(started):
            return True
        created = row["source_created_at"]
        young = created is not None and created > to_db_text(started - FEE_SETTLES)
        return young and row["fee_minor"] is None

    def _payments(
        self, ledger: Mapping[PaymentKey, Mapping[str, Any]], skipped: set[PaymentKey], tab: Rows
    ) -> tuple[int, list[Finding]]:
        """Each payment against its Payments row; the payments compared and the findings."""
        found: dict[str, list[tuple[int, list[Json]]]] = {}
        for at, cells in tab:
            key = _text(_cell(cells, self._key))
            if key:
                found.setdefault(key, []).append((at, cells))
        findings: list[Finding] = []
        checked = 0
        for key in sorted(ledger):
            payment_id = key[2]
            rows = found.pop(payment_id, [])
            if key in skipped:
                continue
            try:
                expected = [column.cell(ledger[key]) for column in self._columns]
            except UnknownCurrency:
                continue  # held back by the flusher, its job dead: not the sheet's fault
            checked += 1
            if not rows:
                findings.append(Finding("sheet_missing", payment_id, repair=payment_id))
                continue
            if len(rows) > 1:
                findings.append(Finding("sheet_duplicate", payment_id, _rows_named(rows)))
            cells = rows[0][1]
            differs = [
                column.header
                for i, column in enumerate(self._columns)
                if _written(_cell(cells, i)) != _written(expected[i])
            ]
            if differs:
                findings.append(
                    Finding("sheet_differs", payment_id, ", ".join(differs), repair=payment_id)
                )
        for payment_id, rows in sorted(found.items()):
            findings.append(Finding("sheet_foreign", payment_id, _rows_named(rows)))
        return checked, findings

    def _summary(
        self, ledger: Mapping[PaymentKey, Mapping[str, Any]], skipped: set[PaymentKey], tab: Rows
    ) -> list[Finding]:
        """Each Daily summary row, recomputed from the ledger, against the tab's; a key no
        longer in the ledger should read zero, as the flusher leaves it."""
        totals = summarise(ledger.values(), self._zone)
        busy = {
            day_of(ledger[key]["source_created_at"], self._zone)
            for key in skipped
            if ledger[key]["source_created_at"]
        }
        on_day: dict[date, str] = {}
        for key in sorted(ledger):
            created = ledger[key]["source_created_at"]
            if created:
                on_day.setdefault(day_of(created, self._zone), key[2])
        found: dict[SummaryKey, list[Json]] = {}
        for _, cells in tab:
            summary_key = read_key(cells)
            if summary_key is not None:
                found.setdefault(summary_key, cells)
        findings: list[Finding] = []
        wanted = {k for k, t in totals.items() if t.payments} | set(found)
        for summary_key in sorted(wanted):
            if summary_key[0] in busy:
                continue
            expected = summary_cells(summary_key, totals.get(summary_key, Totals()))
            if expected is None:
                continue  # an unknown currency: held back with its payments
            cells = found.get(summary_key, [])
            same = all(
                _written(_cell(cells, i)) == _written(cell) for i, cell in enumerate(expected)
            )
            if not same:
                findings.append(
                    Finding(
                        "summary_differs",
                        None,
                        _summary_detail(summary_key),
                        repair=on_day.get(summary_key[0]),
                    )
                )
        return findings

    # -- the database -------------------------------------------------------------------------

    async def _tabs(self) -> tuple[int, int] | None:
        """The stored Payments and Daily summary tab ids, or None before the first flush."""
        async with self._writer.transaction() as tx:
            found = await tx.execute(
                sa.select(sheet_state.c.payments_tab_id, sheet_state.c.daily_summary_tab_id).where(
                    sheet_state.c.spreadsheet_id == self._config.sheets.spreadsheet_id
                )
            )
        if not found or None in (found[0]["payments_tab_id"], found[0]["daily_summary_tab_id"]):
            return None
        return found[0]["payments_tab_id"], found[0]["daily_summary_tab_id"]

    async def _ledger(
        self,
    ) -> tuple[dict[PaymentKey, Mapping[str, Any]], set[PaymentKey]]:
        """Every payment, and the keys of those with a live job of any stage."""
        async with self._writer.transaction() as tx:
            rows = await tx.execute(sa.select(payments))
            live = await tx.execute(
                sa.select(jobs.c.source, jobs.c.account, jobs.c.payment_key)
                .where(jobs.c.status.in_(LIVE_STATUSES))
                .distinct()
            )
        ledger: dict[PaymentKey, Mapping[str, Any]] = {
            (r["source"], r["account"], r["payment_id"]): dict(r) for r in rows
        }
        return ledger, {(r["source"], r["account"], r["payment_key"]) for r in live}

    async def _record(
        self, started: datetime, checked: int, findings: Sequence[Finding]
    ) -> tuple[int, int]:
        """The run, its findings and their repair jobs, in one transaction; the run's id and
        how many findings were repaired."""
        source = RAZORPAY
        account = self._config.razorpay.account
        now = utc_now()
        line = status_line(started, self._zone, checked, len(findings))
        repaired = sum(1 for f in findings if f.repair is not None)
        async with self._writer.transaction() as tx:
            [run] = await tx.execute(
                sa.insert(verify_runs)
                .values(
                    kind="quick",
                    started_at=to_db_text(started),
                    finished_at=to_db_text(now),
                    window_days=0,
                    local_date=started.astimezone(self._zone).date().isoformat(),
                    payments_checked=checked,
                    findings_count=len(findings),
                    repaired_count=repaired,
                    ignored_events=0,
                    outcome="differences" if findings else "clean",
                    status_line=line,
                )
                .returning(verify_runs.c.id)
            )
            for finding in findings:
                job_id = None
                if finding.repair is not None:
                    # Coalesced: two findings of one payment share its one live job.
                    job_id = await enqueue(
                        tx,
                        stage=STAGE,
                        sink=SINK,
                        source=source,
                        account=account,
                        payment_key=finding.repair,
                        event_id=None,
                        retry_profile=self._config.service.retry_profile,
                        now=now,
                    )
                await tx.execute(
                    sa.insert(verify_findings).values(
                        run_id=run["id"],
                        kind=finding.kind,
                        source=source,
                        account=account,
                        payment_id=finding.payment_id,
                        detail=finding.detail,
                        repair_job_id=job_id,
                    )
                )
        return run["id"], repaired


def status_line(started: datetime, zone: ZoneInfo, checked: int, differences: int) -> str:
    """The quick run's Sync status line (Q153)."""
    local = started.astimezone(zone)
    return (
        f"Sheet checked {local:%d %b %H:%M} {zone_label(zone)}: "
        f"{_counted(checked, 'payment')}, {_counted(differences, 'difference')}"
    )


def until_next(now: datetime, zone: ZoneInfo, every: timedelta) -> float:
    """Seconds from `now` to the next multiple of `every` after local midnight in `zone`."""
    local = now.astimezone(zone).replace(tzinfo=None)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    boundary = midnight + ((local - midnight) // every + 1) * every
    at = boundary.replace(tzinfo=zone).astimezone(UTC)
    while at <= now:  # a wall time a daylight-saving change skipped
        at += every
    return (at - now).total_seconds()


def _counted(n: int, noun: str) -> str:
    return f"{n:,} {noun}" + ("" if n == 1 else "s")


def _rows(sheet_id: int, width: int) -> Json:
    """A tab's rows below the header, our columns only."""
    return {
        "gridRange": {
            "sheetId": sheet_id,
            "startRowIndex": 1,
            "startColumnIndex": 0,
            "endColumnIndex": width,
        }
    }


def _by_tab(answer: Json) -> dict[int, Rows]:
    tabs: dict[int, Rows] = {}
    for sheet in answer.get("sheets", []):
        rows: Rows = []
        for data in sheet.get("data", []):
            first = data.get("startRow", 0)
            for offset, row in enumerate(data.get("rowData", [])):
                rows.append((first + offset, row.get("values") or []))
        tabs[sheet.get("properties", {}).get("sheetId", 0)] = rows
    return tabs


def _written(cell: Json) -> Json:
    """What the flusher writes of a cell: its value and number format."""
    out: Json = {}
    if value := cell.get("userEnteredValue"):
        out["value"] = value
    if number_format := cell.get("userEnteredFormat", {}).get("numberFormat"):
        out["format"] = number_format
    return out


def _cell(cells: Sequence[Json], i: int) -> Json:
    """A row's cell `i`; {} for one left out of the read as a trailing empty cell."""
    return cells[i] if i < len(cells) else {}


def _text(cell: Json) -> str | None:
    value: object = cell.get("userEnteredValue", {}).get("stringValue")
    return value if isinstance(value, str) else None


def _rows_named(rows: Sequence[tuple[int, list[Json]]]) -> str:
    """Rows as a person reads them, numbered from 1, the header row being 1: `row 5`,
    `rows 2, 4`."""
    numbers = ", ".join(str(at + 1) for at, _ in rows)
    return f"row {numbers}" if len(rows) == 1 else f"rows {numbers}"


def _summary_detail(key: SummaryKey) -> str:
    day, currency, paid_out = key
    return f"{day.isoformat()} {currency}/{paid_out}"
