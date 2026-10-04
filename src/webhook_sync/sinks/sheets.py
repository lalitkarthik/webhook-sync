"""The flusher: mirror jobs drained into the Google Sheet (sheets-sink.md "The flush").

A batch stage on the stage runner (A12): every 3 s while mirror jobs wait, or at once when 200
do, it claims up to 200 and makes one flush of them all:

1. the first time, with no tab ids stored, create the three tabs (header, header note, frozen
   header row and key columns), adopting any already named so whose header matches ours (one
   that differs pauses "layout changed", nothing written), and store their ids and a
   deployment id;
2. read the grid sizes, header rows and key columns of Payments and Daily summary by tab id,
   in one call; any read failure fails every job with its class (a pause keeps them waiting,
   attempts refunded). A missing Payments or Daily summary pauses "tab missing", a missing
   Sync status is made again; a header that differs in our columns pauses "layout changed";
3. map key → row on each (the last key row is the last used row; a duplicated key: the first);
4. load each job's current ledger row, never the event (ADR 0001), and every succeeded
   payment of the days they touch;
5. one `batchUpdate`: `updateCells` for rows already there without the key cells, appends with
   the key after the last used row, `appendDimension` first when the grid is too small; the
   same for the touched Daily summary rows, recomputed (A15); Sync status rewritten whole.
   A 400 naming `requests[N]` of one payment's row sends that job dead, and the batch is
   rebuilt without it and written again (G70); every other failure fails every job;
6. the post-write check: one read of the key cells of every Payments row written; a row a
   person moved (sorted) between the read and the write shows another key, so the expected
   and the found key get mirror jobs, one `sheet_changed` alert, `sinks.moved_in_a_row` + 1
   and the moved jobs retry; a second flush in a row pauses "sheet changed during write",
   and a clean flush resets the count (Q63, Q143, A14);
7. complete every job and update `sheet_state` in one transaction.

Sync status's Last verify is the latest verify run's line (A17). A verify run asks for a
status-only flush (`BatchStageRunner.request`): the same flush with no jobs, so the line appears
without a mirror job.

A payment in an unknown currency is held back: its job alone goes dead, with one
`unknown_currency` alert naming the code and payment ids; the rest are written.

Every write is by key, so a flush done twice changes nothing: a write whose reply was lost
fails as `retry`, and the next flush finds the rows it wrote and updates them in place.
Nothing here logs the spreadsheet id, an email or the service-account address.
"""

import logging
import random
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from webhook_sync.alerts import Alerter
from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.config import Config, RetryProfile
from webhook_sync.crashpoints import crash_point
from webhook_sync.db.schema import jobs, payments, sheet_state, sinks, verify_runs
from webhook_sync.db.writer import Transaction, Writer
from webhook_sync.queue import ClaimedJob, Done, Failure, Pauses, StageFailed, enqueue
from webhook_sync.queue.runner import BatchStageRunner, BatchWork, Sleep
from webhook_sync.sinks.encoding import UnknownCurrency, payment_columns
from webhook_sync.sinks.sheets_client import TAB_MISSING, Json, SheetsClient, SheetsFailure
from webhook_sync.sinks.summary import (
    KEY_COLUMNS,
    SUCCEEDED,
    SummaryKey,
    SyncStatus,
    Totals,
    day_bounds,
    day_of,
    read_key,
    state_text,
    summarise,
    summary_cells,
    sync_status_rows,
)

log = logging.getLogger("webhook_sync.sheets")

SINK = "sheets"
RAZORPAY = "razorpay"
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

LAYOUT_CHANGED = "layout changed"
"""The pause reason when a header row differs from ours in our columns (G8, Q140)."""

SHEET_CHANGED = "sheet changed during write"
"""The pause reason when rows moved under two flushes in a row (Q63, A14)."""

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
    alerter: Alerter | None = None,
) -> BatchStageRunner:
    """The flusher: the `mirror` stage of the `sheets` sink, as a batch stage on the runner."""
    flusher = Flusher(writer, client, config, alerter=alerter)
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

    def __init__(
        self,
        writer: Writer,
        client: SheetsClient,
        config: Config,
        *,
        alerter: Alerter | None = None,
    ) -> None:
        self._writer = writer
        self._client = client
        self._retry_profile: RetryProfile = config.service.retry_profile
        self._alerter = alerter
        self._spreadsheet_id = config.sheets.spreadsheet_id
        self._zone = config.service.timezone
        self._columns = payment_columns(config)
        self._key = next(i for i, c in enumerate(self._columns) if c.key == "payment_id")

    @property
    def work(self) -> BatchWork:
        return self.flush

    async def flush(self, claimed: list[ClaimedJob]) -> Done:
        started = time.perf_counter()
        state = await self._state()
        read = await self._read(state.tabs)
        tabs = read.tabs
        rows = await self._ledger_rows(claimed)
        cells: dict[int, list[Json]] = {}
        failed: dict[int, Failure] = {}
        held: dict[str, list[str]] = {}
        for job in claimed:
            row = rows.get((job.source, job.account, job.payment_key))
            if row is None:
                continue  # nothing in the ledger to mirror
            try:
                cells[job.id] = self._cells(row)
            except UnknownCurrency as unknown:
                # Held back, never written 100 times off (Q62): only this job goes dead.
                failed[job.id] = Failure(str(unknown), "dead")
                held.setdefault(unknown.code, []).append(job.payment_key)
        summary = await self._summary(tabs.daily_summary, read, rows.values())
        summary_rows = sum(1 for r in summary if "updateCells" in r)
        now = utc_now()
        while True:
            writing = [job for job in claimed if job.id in cells and job.id not in failed]
            rows_written = len(rows) - len(failed)
            status = await self._status(state, rows_written, failed, now)
            payment_requests, owners, placed = self._payment_requests(
                tabs.payments, read, writing, cells
            )
            requests = [
                *payment_requests,
                *summary,
                _update_cells(tabs.sync_status, 1, 0, sync_status_rows(status, self._zone)),
            ]
            written = await self._client.batch_update(requests)
            if not isinstance(written, SheetsFailure):
                break
            index = written.request_index
            owner = owners[index] if index is not None and index < len(owners) else None
            if written.error_class != "dead" or owner is None:
                _log_flush(claimed, 0, 0, 0, started, outcome=written.error_class)
                raise StageFailed(written)
            # One payment's row is bad (G70): its job alone goes dead; the batch is rebuilt
            # without it and written again.
            failed[owner] = written
        appended = sum(1 for job in writing if job.payment_key not in read.payment_keys)
        moved = await self._moved(tabs.payments, placed)
        _log_flush(
            claimed,
            rows_written - appended,
            appended,
            summary_rows,
            started,
            outcome="moved" if moved else "ok",
        )
        if held and self._alerter is not None:
            await self._alerter.unknown_currency(held)
        if moved:
            for job, _ in moved:
                failed[job.id] = Failure(SHEET_CHANGED, "retry")
            if await self._repair(moved) > 1:
                raise StageFailed(Failure(SHEET_CHANGED, "pause", pause_target="sink"))

        async def writes(tx: Transaction) -> None:
            if not moved:
                await tx.execute(
                    sa.update(sinks).where(sinks.c.name == SINK).values(moved_in_a_row=0)
                )
            await tx.execute(
                sa.update(sheet_state)
                .where(sheet_state.c.spreadsheet_id == self._spreadsheet_id)
                .values(
                    last_flush_at=to_db_text(now),
                    rows_written_last=rows_written,
                    rows_written_total=sheet_state.c.rows_written_total + rows_written,
                )
            )

        crash_point("after_sheet_write_before_done")  # written and checked, jobs not completed
        return Done(writes=writes, failed=failed)

    # -- the post-write check ---------------------------------------------------------------

    async def _moved(
        self, sheet_id: int, placed: Mapping[int, ClaimedJob]
    ) -> list[tuple[ClaimedJob, str | None]]:
        """The post-write check (sheets-sink.md step 9, Q63): one read of the key cells of every
        Payments row just written. Updates never write the key cell, so a row a person moved
        between the read and the write shows another key than expected (F05). Each job whose
        row did, with the key found there (None for an empty one)."""
        if not placed:
            return []
        first, last = min(placed), max(placed)
        answer = _answered(
            await self._client.get_by_data_filter(
                [_key_columns(sheet_id, self._key, self._key + 1, first, last + 1)],
                fields=KEY_COLUMN_FIELDS,
            )
        )
        found: dict[int, str] = {}
        for sheet in answer.get("sheets", []):
            for data in sheet.get("data", []):
                start = data.get("startRow", 0)
                for offset, row in enumerate(data.get("rowData", [])):
                    key = _payment_key(row.get("values") or [])
                    if key is not None:
                        found[start + offset] = key
        return [
            (job, found.get(at))
            for at, job in sorted(placed.items())
            if found.get(at) != job.payment_key
        ]

    async def _repair(self, moved: Sequence[tuple[ClaimedJob, str | None]]) -> int:
        """Every key involved gets a mirror job, the one expected and the one found (Q143),
        `moved_in_a_row` goes up by one, and one `sheet_changed` alert says what happened.
        Returns the new `moved_in_a_row`."""
        now = utc_now()
        involved: dict[tuple[str, str, str], None] = {}
        for job, found in moved:
            involved[(job.source, job.account, job.payment_key)] = None
            if found is not None:
                involved[(job.source, job.account, found)] = None
        count = sqlite.insert(sinks).values(
            name=SINK, kind="sink", state="active", moved_in_a_row=1
        )
        async with self._writer.transaction() as tx:
            for source, account, payment_key in involved:
                await enqueue(
                    tx,
                    stage=STAGE,
                    sink=SINK,
                    source=source,
                    account=account,
                    payment_key=payment_key,
                    event_id=None,
                    retry_profile=self._retry_profile,
                    now=now,
                )
            [row] = await tx.execute(
                count.on_conflict_do_update(
                    index_elements=[sinks.c.name],
                    set_={"moved_in_a_row": sinks.c.moved_in_a_row + 1},
                ).returning(sinks.c.moved_in_a_row)
            )
        keys = ", ".join(sorted({key for _, _, key in involved}))
        log.warning("sheet changed during write", extra={"rows_moved": len(moved)})
        if self._alerter is not None:
            await self._alerter.send(
                "sheet_changed",
                f"{len(moved)} Payments row(s) moved during a write; "
                f"queued again to rewrite: {keys}",
            )
        return row["moved_in_a_row"]

    # -- Daily summary and Sync status -----------------------------------------------------------

    async def _summary(
        self, sheet_id: int, read: _Read, flushed: Iterable[Mapping[str, Any]]
    ) -> list[Json]:
        """The Daily summary rows of every day the flushed payments touch, recomputed from the
        ledger (A15): rows already on the tab updated in place around their key, new days
        appended after the last used row, and a key no longer in the ledger set to zero."""
        days = sorted(
            {day_of(r["source_created_at"], self._zone) for r in flushed if r["source_created_at"]}
        )
        if not days:
            return []
        ranges = [day_bounds(day, self._zone) for day in days]
        async with self._writer.transaction() as tx:
            found = await tx.execute(
                sa.select(payments).where(
                    payments.c.status.in_(SUCCEEDED),
                    sa.or_(
                        *(
                            sa.and_(
                                payments.c.source_created_at >= start,
                                payments.c.source_created_at < end,
                            )
                            for start, end in ranges
                        )
                    ),
                )
            )
        totals = summarise([dict(r) for r in found], self._zone)
        touched = set(days)
        for key in read.summary_keys:
            if key[0] in touched:
                totals.setdefault(key, Totals())
        updates: list[Json] = []
        appended: list[list[Json]] = []
        for key in sorted(totals):
            cells = summary_cells(key, totals[key])
            if cells is None:
                continue  # an unknown currency: held back with its payments
            at = read.summary_keys.get(key)
            if at is None:
                if totals[key].payments:
                    appended.append(cells)
            else:
                updates.append(_update_cells(sheet_id, at, KEY_COLUMNS, [cells[KEY_COLUMNS:]]))
        return [
            *updates,
            *self._append(sheet_id, appended, read.summary_last, read.summary_rows),
        ]

    async def _status(
        self, state: _State, rows_written: int, failed: Mapping[int, Failure], now: datetime
    ) -> SyncStatus:
        """Sync status as this flush leaves things."""
        async with self._writer.transaction() as tx:
            waiting = await tx.execute(
                sa.select(sa.func.count().label("n"))
                .select_from(jobs)
                .where(jobs.c.sink == SINK, jobs.c.stage == STAGE, jobs.c.status == "waiting")
            )
            dead = await tx.execute(
                sa.select(sa.func.count().label("n"))
                .select_from(jobs)
                .where(jobs.c.status == "dead")
            )
            states = await tx.execute(
                sa.select(sinks.c.name, sinks.c.state, sinks.c.reason).where(
                    sinks.c.name.in_([SINK, RAZORPAY])
                )
            )
            verified = await tx.execute(
                sa.select(verify_runs.c.status_line)
                .where(verify_runs.c.status_line.is_not(None))
                .order_by(verify_runs.c.id.desc())
                .limit(1)
            )
        by_name = {r["name"]: state_text(r["state"], r["reason"]) for r in states}
        newly_dead = sum(1 for f in failed.values() if f.error_class == "dead")
        return SyncStatus(
            last_flush=now,
            rows_written_last=rows_written,
            rows_written_total=state.rows_written_total + rows_written,
            jobs_waiting=waiting[0]["n"],
            dead_jobs=dead[0]["n"] + newly_dead,
            sheet_state=by_name.get(SINK, "active"),
            razorpay_state=by_name.get(RAZORPAY, "active"),
            deployment_id=state.deployment_id,
            last_verify=verified[0]["status_line"] if verified else None,
        )

    # -- tabs ----------------------------------------------------------------------------------

    async def _state(self) -> _State:
        """`sheet_state`: the three tabs' ids (stored ones, or, on the first run, set up and
        then stored), the deployment id (made once) and the rows written so far."""
        stored = await self._stored()
        if stored is not None and stored.tabs is not None and stored.deployment_id:
            return _State(stored.tabs, stored.deployment_id, stored.rows_written_total)
        tabs = stored.tabs if stored is not None and stored.tabs else await self._set_up_tabs()
        deployment_id = stored.deployment_id if stored is not None else None
        insert = sqlite.insert(sheet_state).values(
            spreadsheet_id=self._spreadsheet_id,
            payments_tab_id=tabs.payments,
            daily_summary_tab_id=tabs.daily_summary,
            sync_status_tab_id=tabs.sync_status,
            deployment_id=deployment_id or uuid.uuid4().hex,
        )
        async with self._writer.transaction() as tx:
            await tx.execute(
                insert.on_conflict_do_update(
                    index_elements=[sheet_state.c.spreadsheet_id],
                    set_={
                        "payments_tab_id": insert.excluded.payments_tab_id,
                        "daily_summary_tab_id": insert.excluded.daily_summary_tab_id,
                        "sync_status_tab_id": insert.excluded.sync_status_tab_id,
                        "deployment_id": sa.func.coalesce(
                            sheet_state.c.deployment_id, insert.excluded.deployment_id
                        ),
                    },
                )
            )
        stored = await self._stored()
        assert stored is not None
        return _State(tabs, stored.deployment_id, stored.rows_written_total)

    async def _stored(self) -> _Stored | None:
        async with self._writer.transaction() as tx:
            found = await tx.execute(
                sa.select(sheet_state).where(sheet_state.c.spreadsheet_id == self._spreadsheet_id)
            )
        if not found:
            return None
        row = found[0]
        ids = [row["payments_tab_id"], row["daily_summary_tab_id"], row["sync_status_tab_id"]]
        return _Stored(
            TabIds(*ids) if None not in ids else None,
            row["deployment_id"],
            row["rows_written_total"],
        )

    async def _set_up_tabs(self) -> TabIds:
        """First run (Q140): a tab already named Payments, Daily summary or Sync status is
        adopted when its header matches ours; when one differs, the flush pauses "layout
        changed" before anything is written or stored. A missing tab is created with its
        header, header note, frozen header row and key column, all in one `batchUpdate`."""
        answer = _answered(await self._client.get(fields="sheets.properties(sheetId,title)"))
        existing: dict[str, int] = {}
        for sheet in answer.get("sheets", []):
            properties = sheet.get("properties", {})
            existing[properties.get("title")] = properties.get("sheetId", 0)
        taken = set(existing.values())
        specs = self._specs()
        adopted = [(existing[s.title], s) for s in specs if s.title in existing]
        if adopted:
            headers = _answered(
                await self._client.get_by_data_filter(
                    [_header(sheet_id, len(spec.header)) for sheet_id, spec in adopted],
                    fields=KEY_COLUMN_FIELDS,
                )
            )
            found = {
                s.get("properties", {}).get("sheetId", 0): s for s in headers.get("sheets", [])
            }
            for sheet_id, spec in adopted:
                if _header_of(found.get(sheet_id, {})) != spec.header:
                    raise StageFailed(_layout_changed())
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

    def _specs(self) -> tuple[_TabSpec, _TabSpec, _TabSpec]:
        return (
            _TabSpec(PAYMENTS, tuple(c.header for c in self._columns), self._key + 1),
            _TabSpec(DAILY_SUMMARY, DAILY_SUMMARY_HEADER, 3),
            _TabSpec(SYNC_STATUS, SYNC_STATUS_HEADER, 1),
        )

    async def _read(self, tabs: TabIds) -> _Read:
        """One read by tab id: the header rows, key columns and grid sizes of Payments and
        Daily summary, and Sync status's header row (only to see it is there).

        A missing Payments or Daily summary tab pauses the sink, "tab missing"; a missing
        Sync status is made again and the read repeated. A header that differs from ours in
        our columns (extra columns to the right are not read) pauses it, "layout changed"."""
        payments_spec, summary_spec, status_spec = self._specs()
        filters = [
            _header(tabs.payments, len(payments_spec.header)),
            _key_columns(tabs.payments, self._key, self._key + 1),
            _header(tabs.daily_summary, len(summary_spec.header)),
            _key_columns(tabs.daily_summary, 0, KEY_COLUMNS),
            _header(tabs.sync_status, len(status_spec.header)),
        ]
        answer = await self._client.get_by_data_filter(filters, fields=KEY_COLUMN_FIELDS)
        if isinstance(answer, SheetsFailure) and answer.error == TAB_MISSING:
            tabs = await self._sync_status_again(tabs, answer)
            return await self._read(tabs)
        found = {
            s.get("properties", {}).get("sheetId", 0): s
            for s in _answered(answer).get("sheets", [])
        }
        if any(sheet_id not in found for sheet_id in (tabs.payments, tabs.daily_summary)):
            raise StageFailed(Failure(TAB_MISSING, "pause", pause_target="sink"))
        for sheet_id, spec in ((tabs.payments, payments_spec), (tabs.daily_summary, summary_spec)):
            if _header_of(found[sheet_id]) != spec.header:
                raise StageFailed(_layout_changed())

        payment_keys, payments_last, payments_rows = _keys(found[tabs.payments], _payment_key)
        summary_keys, summary_last, summary_rows = _keys(found[tabs.daily_summary], read_key)
        return _Read(
            tabs,
            payment_keys,
            payments_last,
            payments_rows,
            summary_keys,
            summary_last,
            summary_rows,
        )

    async def _sync_status_again(self, tabs: TabIds, missing: SheetsFailure) -> TabIds:
        """After a read found a tab missing: when only Sync status is gone, make it again
        (G10) and store its new id; otherwise the flush fails with `missing`, a pause."""
        answer = _answered(await self._client.get(fields="sheets.properties(sheetId,title)"))
        present = {s.get("properties", {}).get("sheetId", 0) for s in answer.get("sheets", [])}
        if tabs.payments not in present or tabs.daily_summary not in present:
            raise StageFailed(missing)
        if tabs.sync_status in present:
            raise StageFailed(missing)  # not ours to mend: something else went missing
        sheet_id = _new_sheet_id(present)
        _answered(await self._client.batch_update(_new_tab(sheet_id, self._specs()[2])))
        async with self._writer.transaction() as tx:
            await tx.execute(
                sa.update(sheet_state)
                .where(sheet_state.c.spreadsheet_id == self._spreadsheet_id)
                .values(sync_status_tab_id=sheet_id)
            )
        log.warning("Sync status tab made again")
        return replace(tabs, sync_status=sheet_id)

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

    def _payment_requests(
        self,
        sheet_id: int,
        read: _Read,
        writing: Sequence[ClaimedJob],
        cells: Mapping[int, list[Json]],
    ) -> tuple[list[Json], list[int | None], dict[int, ClaimedJob]]:
        """The Payments tab's requests and, for each, the job whose row it writes (None for
        the grid growing): updates around the key cell, then one request per appended row, so
        that a 400 naming `requests[N]` names one job. Also each row written, with its job."""
        requests: list[Json] = []
        owners: list[int | None] = []
        appended: list[ClaimedJob] = []
        placed: dict[int, ClaimedJob] = {}
        for job in writing:
            at = read.payment_keys.get(job.payment_key)
            if at is None:
                appended.append(job)
                continue
            placed[at] = job
            for request in self._update(sheet_id, at, cells[job.id]):
                requests.append(request)
                owners.append(job.id)
        first = read.payments_last + 1
        grow = _grow(sheet_id, first + len(appended) - read.payments_rows)
        if appended and grow is not None:
            requests.append(grow)
            owners.append(None)
        for offset, job in enumerate(appended):
            requests.append(_update_cells(sheet_id, first + offset, 0, [cells[job.id]]))
            owners.append(job.id)
            placed[first + offset] = job
        return requests, owners, placed

    def _cells(self, row: Mapping[str, Any]) -> list[Json]:
        return [column.cell(row) for column in self._columns]

    def _update(self, sheet_id: int, at: int, cells: Sequence[Json]) -> list[Json]:
        """A row already on the tab, rewritten around its key cell, never through it (F05)."""
        parts = [(0, cells[: self._key]), (self._key + 1, cells[self._key + 1 :])]
        return [_update_cells(sheet_id, at, column, [part]) for column, part in parts if part]

    def _append(
        self, sheet_id: int, rows: Sequence[Sequence[Json]], last_used: int, row_count: int
    ) -> list[Json]:
        """New rows, key included, straight after the last used row; the grid grown first
        when it is too small (F07)."""
        if not rows:
            return []
        first = last_used + 1
        grow = _grow(sheet_id, first + len(rows) - row_count)
        return [*([grow] if grow is not None else []), _update_cells(sheet_id, first, 0, rows)]


@dataclass(frozen=True)
class _Stored:
    tabs: TabIds | None
    deployment_id: str | None
    rows_written_total: int


@dataclass(frozen=True)
class _State:
    tabs: TabIds
    deployment_id: str | None
    rows_written_total: int


@dataclass(frozen=True)
class _Read:
    """What the one read found: the tab ids it read by (Sync status's new one if it was made
    again), and each tab's key → row index (the first row holding it), its last used row (0,
    the header, when none) and its row count."""

    tabs: TabIds
    payment_keys: dict[str, int]
    payments_last: int
    payments_rows: int
    summary_keys: dict[SummaryKey, int]
    summary_last: int
    summary_rows: int


def _header(sheet_id: int, width: int) -> Json:
    """A tab's header row, our columns only: an extra column to the right is never read."""
    return {
        "gridRange": {
            "sheetId": sheet_id,
            "startRowIndex": 0,
            "endRowIndex": 1,
            "startColumnIndex": 0,
            "endColumnIndex": width,
        }
    }


def _key_columns(
    sheet_id: int, first: int, end: int, first_row: int = 1, end_row: int | None = None
) -> Json:
    """A tab's key columns below the header, or rows `first_row` up to `end_row` of them (a
    range the read clips to the grid)."""
    grid: Json = {
        "sheetId": sheet_id,
        "startRowIndex": first_row,
        "startColumnIndex": first,
        "endColumnIndex": end,
    }
    if end_row is not None:
        grid["endRowIndex"] = end_row
    return {"gridRange": grid}


def _payment_key(values: list[Json]) -> str | None:
    key: object = (values or [{}])[0].get("effectiveValue", {}).get("stringValue")
    return key if isinstance(key, str) and key else None


def _header_of(sheet: Json) -> tuple[str, ...] | None:
    """The header row's texts as a read found them: the range starting at row 0 (a key
    range starts at row 1); None when the read holds no header range."""
    for data in sheet.get("data", []):
        if data.get("startRow", 0) == 0:
            rows: list[Json] = data.get("rowData") or [{}]
            values: list[Json] = rows[0].get("values") or []
            return tuple(_text(value) for value in values)
    return None


def _text(cell: Json) -> str:
    value: object = cell.get("effectiveValue", {}).get("stringValue")
    return value if isinstance(value, str) else ""


def _layout_changed() -> Failure:
    return Failure(LAYOUT_CHANGED, "pause", pause_target="sink")


def _keys[K](
    sheet: Json, key_of: Callable[[list[Json]], K | None]
) -> tuple[dict[K, int], int, int]:
    row_count: int = sheet["properties"].get("gridProperties", {}).get("rowCount", 0)
    keys: dict[K, int] = {}
    last_used = 0
    for data in sheet.get("data", []):
        first = data.get("startRow", 0)
        for offset, row in enumerate(data.get("rowData", [])):
            at = first + offset
            if at == 0:
                continue
            key = key_of(row.get("values") or [])
            if key is None:
                continue
            keys.setdefault(key, at)
            last_used = max(last_used, at)
    return keys, last_used, row_count


def _grow(sheet_id: int, short: int) -> Json | None:
    """`appendDimension` for the rows a tab's grid is `short` of, or None when it isn't."""
    if short <= 0:
        return None
    return {"appendDimension": {"sheetId": sheet_id, "dimension": "ROWS", "length": short}}


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
    claimed: Sequence[ClaimedJob],
    updated: int,
    appended: int,
    summary_rows: int,
    started: float,
    *,
    outcome: str,
) -> None:
    log.info(
        "sheet flush",
        extra={
            "jobs": len(claimed),
            "rows_updated": updated,
            "rows_appended": appended,
            "summary_rows": summary_rows,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "outcome": outcome,
        },
    )
