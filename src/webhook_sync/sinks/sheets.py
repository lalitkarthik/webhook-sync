"""The flusher: mirror jobs drained into the Google Sheet (sheets-sink.md "The flush").

A batch stage on the stage runner (A12): every 3 s while mirror jobs wait, or at once when 200
do, it claims up to 200 and makes one flush of them all:

1. the first time, with no tab ids stored, create the three tabs (header, header note, frozen
   header row and key column), adopting any already named so, and store their ids;
2. read the Payments tab's grid size and key column by tab id, in one call; any read failure
   fails every job with its class;
3. map key → row (the last key row is the last used row; a duplicated key: the first);
4. load each job's current ledger row, never the event (ADR 0001);
5. one `batchUpdate`: `updateCells` for rows already there without the key cell, appends with
   the key after the last used row, `appendDimension` first when the grid is too small;
6. complete every job and update `sheet_state` in one transaction.

Every write is by key, so a flush done twice changes nothing: a write whose reply was lost
fails as `retry`, and the next flush finds the rows it wrote and updates them in place.
Nothing here logs the spreadsheet id, an email or the service-account address.
"""

import logging
import random
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.config import Config
from webhook_sync.db.schema import payments, sheet_state
from webhook_sync.db.writer import Transaction, Writer
from webhook_sync.queue import ClaimedJob, Done, Failure, Pauses, StageFailed
from webhook_sync.queue.runner import BatchStageRunner, BatchWork, Sleep
from webhook_sync.sinks.encoding import payment_columns
from webhook_sync.sinks.sheets_client import TAB_MISSING, Json, SheetsClient, SheetsFailure

log = logging.getLogger("webhook_sync.sheets")

SINK = "sheets"
STAGE = "mirror"

FLUSH_LIMIT = 200
"""The most mirror jobs one flush carries; this many waiting flushes at once."""

FLUSH_EVERY = timedelta(seconds=3)
"""How often the flusher runs while fewer than `FLUSH_LIMIT` jobs wait."""

PAYMENTS = "Payments"
DAILY_SUMMARY = "Daily summary"
SYNC_STATUS = "Sync status"

HEADER_NOTE = (
    "Managed by webhook-sync. Sort and filter with Data › Create filter view; put your own "  # noqa: RUF001
    "work in another tab."
)

DAILY_SUMMARY_HEADER = (
    "Day",
    "Currency",
    "Fee currency",
    "Payments",
    "Gross",
    "Refunds",
    "Fees",
    "Net (before refunds)",
    "Fee pending",
)
SYNC_STATUS_HEADER = ("Label", "Value")

NEW_TAB_ROWS = 1000
NEW_TAB_COLUMNS = 26
CELL_FIELDS = "userEnteredValue,userEnteredFormat.numberFormat"
KEY_COLUMN_FIELDS = (
    "sheets(properties(sheetId,gridProperties(rowCount,columnCount)),"
    "data(startRow,rowData(values(effectiveValue))))"
)


@dataclass(frozen=True)
class TabIds:
    payments: int
    daily_summary: int
    sync_status: int


@dataclass(frozen=True)
class _TabSpec:
    title: str
    header: tuple[str, ...]
    frozen_columns: int


def sheets_runner(
    writer: Writer,
    client: SheetsClient,
    config: Config,
    *,
    claimed_by: str,
    pauses: Pauses,
    rng: random.Random | None = None,
    sleep: Sleep | None = None,
) -> BatchStageRunner:
    """The flusher: the `mirror` stage of the `sheets` sink, as a batch stage on the runner."""
    flusher = Flusher(writer, client, config)
    options: dict[str, Any] = {} if sleep is None else {"sleep": sleep}
    return BatchStageRunner(
        writer,
        stage=STAGE,
        sink=SINK,
        limit=FLUSH_LIMIT,
        every=FLUSH_EVERY,
        work=flusher.work,
        claimed_by=claimed_by,
        pauses=pauses,
        retry_profile=config.service.retry_profile,
        rng=rng,
        **options,
    )


class Flusher:
    """One spreadsheet's flush: the work the batch stage runs on every claimed mirror job."""

    def __init__(self, writer: Writer, client: SheetsClient, config: Config) -> None:
        self._writer = writer
        self._client = client
        self._spreadsheet_id = config.sheets.spreadsheet_id
        self._columns = payment_columns(config)
        self._key = next(i for i, c in enumerate(self._columns) if c.key == "payment_id")

    @property
    def work(self) -> BatchWork:
        return self.flush

    async def flush(self, claimed: list[ClaimedJob]) -> Done:
        started = time.perf_counter()
        tabs = await self._tabs()
        keys, last_used, row_count = await self._read_keys(tabs.payments)
        rows = await self._ledger_rows(claimed)
        updates: list[Mapping[str, Any]] = []
        appended: list[Mapping[str, Any]] = []
        for job in claimed:
            row = rows.get((job.source, job.account, job.payment_key))
            if row is None:
                continue  # nothing in the ledger to mirror
            at = keys.get(job.payment_key)
            if at is None:
                appended.append(row)
            else:
                updates.extend(self._update(tabs.payments, at, row))
        requests = [*updates, *self._append(tabs.payments, appended, last_used, row_count)]
        if requests:
            written = await self._client.batch_update(requests)
            if isinstance(written, SheetsFailure):
                _log_flush(claimed, 0, 0, started, outcome=written.error_class)
                raise StageFailed(written)
        updated = len(rows) - len(appended)
        _log_flush(claimed, updated, len(appended), started, outcome="ok")
        rows_written = len(rows)
        now = utc_now()

        async def writes(tx: Transaction) -> None:
            await tx.execute(
                sa.update(sheet_state)
                .where(sheet_state.c.spreadsheet_id == self._spreadsheet_id)
                .values(
                    last_flush_at=to_db_text(now),
                    rows_written_last=rows_written,
                    rows_written_total=sheet_state.c.rows_written_total + rows_written,
                )
            )

        return Done(writes=writes)

    # -- tabs ----------------------------------------------------------------------------------

    async def _tabs(self) -> TabIds:
        """The three tabs' ids: stored ones, or, on the first run, set up and then stored."""
        async with self._writer.transaction() as tx:
            stored = await tx.execute(
                sa.select(sheet_state).where(sheet_state.c.spreadsheet_id == self._spreadsheet_id)
            )
        if stored:
            ids = [
                stored[0]["payments_tab_id"],
                stored[0]["daily_summary_tab_id"],
                stored[0]["sync_status_tab_id"],
            ]
            if None not in ids:
                return TabIds(*ids)
        tabs = await self._set_up_tabs()
        insert = sqlite.insert(sheet_state).values(
            spreadsheet_id=self._spreadsheet_id,
            payments_tab_id=tabs.payments,
            daily_summary_tab_id=tabs.daily_summary,
            sync_status_tab_id=tabs.sync_status,
        )
        async with self._writer.transaction() as tx:
            await tx.execute(
                insert.on_conflict_do_update(
                    index_elements=[sheet_state.c.spreadsheet_id],
                    set_={
                        "payments_tab_id": insert.excluded.payments_tab_id,
                        "daily_summary_tab_id": insert.excluded.daily_summary_tab_id,
                        "sync_status_tab_id": insert.excluded.sync_status_tab_id,
                    },
                )
            )
        return tabs

    async def _set_up_tabs(self) -> TabIds:
        """First run (Q140): a tab already named Payments, Daily summary or Sync status is
        adopted (its header check comes later); a missing one is created with its header,
        header note, frozen header row and key column, all in one `batchUpdate`."""
        answer = _answered(await self._client.get(fields="sheets.properties(sheetId,title)"))
        existing: dict[str, int] = {}
        for sheet in answer.get("sheets", []):
            properties = sheet.get("properties", {})
            existing[properties.get("title")] = properties.get("sheetId", 0)
        taken = set(existing.values())
        specs = (
            _TabSpec(PAYMENTS, tuple(c.header for c in self._columns), self._key + 1),
            _TabSpec(DAILY_SUMMARY, DAILY_SUMMARY_HEADER, 3),
            _TabSpec(SYNC_STATUS, SYNC_STATUS_HEADER, 1),
        )
        ids: list[int] = []
        requests: list[Json] = []
        for spec in specs:
            if spec.title in existing:
                ids.append(existing[spec.title])
                continue
            sheet_id = _new_sheet_id(taken)
            taken.add(sheet_id)
            ids.append(sheet_id)
            requests.extend(_new_tab(sheet_id, spec))
        if requests:
            _answered(await self._client.batch_update(requests))
        return TabIds(*ids)

    # -- the read -------------------------------------------------------------------------------

    async def _read_keys(self, sheet_id: int) -> tuple[dict[str, int], int, int]:
        """One read by tab id: the key column and the grid size. Returns key → row index (the
        first row holding it), the last used row (0, the header, when none) and the row
        count."""
        answer = _answered(
            await self._client.get_by_data_filter(
                [
                    {
                        "gridRange": {
                            "sheetId": sheet_id,
                            "startColumnIndex": self._key,
                            "endColumnIndex": self._key + 1,
                        }
                    }
                ],
                fields=KEY_COLUMN_FIELDS,
            )
        )
        sheet = next(
            (
                s
                for s in answer.get("sheets", [])
                if s.get("properties", {}).get("sheetId", 0) == sheet_id
            ),
            None,
        )
        if sheet is None:
            raise StageFailed(Failure(TAB_MISSING, "pause", pause_target="sink"))
        row_count: int = sheet["properties"].get("gridProperties", {}).get("rowCount", 0)
        keys: dict[str, int] = {}
        last_used = 0
        for data in sheet.get("data", []):
            first = data.get("startRow", 0)
            for offset, row in enumerate(data.get("rowData", [])):
                at = first + offset
                values: list[Json] = row.get("values") or [{}]
                key: object = values[0].get("effectiveValue", {}).get("stringValue")
                if at == 0 or not isinstance(key, str) or not key:
                    continue
                keys.setdefault(key, at)
                last_used = max(last_used, at)
        return keys, last_used, row_count

    # -- the ledger -----------------------------------------------------------------------------

    async def _ledger_rows(
        self, claimed: Sequence[ClaimedJob]
    ) -> dict[tuple[str, str, str], Mapping[str, Any]]:
        """Each job's payment as the ledger holds it now, by (source, account, payment id)."""
        wanted = [(job.source, job.account, job.payment_key) for job in claimed]
        key = sa.tuple_(payments.c.source, payments.c.account, payments.c.payment_id)
        async with self._writer.transaction() as tx:
            found = await tx.execute(sa.select(payments).where(key.in_(wanted)))
        return {(r["source"], r["account"], r["payment_id"]): dict(r) for r in found}

    # -- the write ------------------------------------------------------------------------------

    def _cells(self, row: Mapping[str, Any]) -> list[Json]:
        return [column.cell(row) for column in self._columns]

    def _update(self, sheet_id: int, at: int, row: Mapping[str, Any]) -> list[Json]:
        """A row already on the tab, rewritten around its key cell, never through it (F05)."""
        cells = self._cells(row)
        parts = [(0, cells[: self._key]), (self._key + 1, cells[self._key + 1 :])]
        return [_update_cells(sheet_id, at, column, [part]) for column, part in parts if part]

    def _append(
        self, sheet_id: int, rows: Sequence[Mapping[str, Any]], last_used: int, row_count: int
    ) -> list[Json]:
        """New rows, key included, straight after the last used row; the grid grown first
        when it is too small (F07)."""
        if not rows:
            return []
        first = last_used + 1
        requests: list[Json] = []
        short = first + len(rows) - row_count
        if short > 0:
            requests.append(
                {"appendDimension": {"sheetId": sheet_id, "dimension": "ROWS", "length": short}}
            )
        requests.append(_update_cells(sheet_id, first, 0, [self._cells(row) for row in rows]))
        return requests


def _update_cells(sheet_id: int, row: int, column: int, rows: Sequence[Sequence[Json]]) -> Json:
    return {
        "updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": row, "columnIndex": column},
            "rows": [{"values": list(values)} for values in rows],
            "fields": CELL_FIELDS,
        }
    }


def _new_tab(sheet_id: int, spec: _TabSpec) -> list[Json]:
    """A new tab: its grid with the header row and key columns frozen, and the header row with
    the header note on its first cell."""
    header: list[Json] = [{"userEnteredValue": {"stringValue": text}} for text in spec.header]
    header[0]["note"] = HEADER_NOTE
    return [
        {
            "addSheet": {
                "properties": {
                    "sheetId": sheet_id,
                    "title": spec.title,
                    "gridProperties": {
                        "rowCount": NEW_TAB_ROWS,
                        "columnCount": max(NEW_TAB_COLUMNS, len(spec.header)),
                        "frozenRowCount": 1,
                        "frozenColumnCount": spec.frozen_columns,
                    },
                }
            }
        },
        {
            "updateCells": {
                "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
                "rows": [{"values": header}],
                "fields": "userEnteredValue,note",
            }
        },
    ]


def _new_sheet_id(taken: set[int]) -> int:
    while True:
        sheet_id = random.randrange(1, 2**31)
        if sheet_id not in taken:
            return sheet_id


def _answered(answer: Json | SheetsFailure) -> Json:
    """A call's answer, or the flush aborted: every job fails with the call's class."""
    if isinstance(answer, SheetsFailure):
        raise StageFailed(answer)
    return answer


def _log_flush(
    claimed: Sequence[ClaimedJob], updated: int, appended: int, started: float, *, outcome: str
) -> None:
    log.info(
        "sheet flush",
        extra={
            "jobs": len(claimed),
            "rows_updated": updated,
            "rows_appended": appended,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "outcome": outcome,
        },
    )
