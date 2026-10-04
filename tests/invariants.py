"""Settle and the nine invariants (testing.md): what every scenario, crash test and load check
ends with.

`settle` waits until no job is waiting or running, and fails at once on a source or sink paused
that the test did not pause on purpose (F22). `observe` then reads what the run left: the
database, the sheet, what FakeRazorpay knows and sent, and every state `LedgerWatch` saw each
ledger row take. Each invariant is a separate check over that `Outcome`, returning what it found
(nothing when it holds); `assert_invariants` runs all nine. What a test broke on purpose it
declares in `OnPurpose`, so the checks don't count it against the service.

Import with `from invariants import ...`.
"""

import asyncio
import contextlib
import copy
import time
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from fakes.razorpay import FakeRazorpay
from fakes.sheets import FakeSheets, Tab

from webhook_sync.config import Config
from webhook_sync.db.reader import Reader
from webhook_sync.ledger.worker import business_columns
from webhook_sync.sinks.encoding import UnknownCurrency, payment_columns
from webhook_sync.sinks.sheets import DAILY_SUMMARY_HEADER
from webhook_sync.sinks.summary import Totals, read_key, summarise, summary_cells
from webhook_sync.sources.razorpay import payment_row

type Json = dict[str, Any]
type Row = Mapping[str, Any]

SETTLE_LIMIT = 30.0
"""Real seconds a run gets to settle, even on a busy machine."""
WATCH_EVERY = 0.005
"""Real seconds between two looks of `LedgerWatch` at the ledger."""

OPEN_JOBS = (
    "SELECT id, stage, sink, source, payment_key, status FROM jobs"
    " WHERE status IN ('waiting', 'running') ORDER BY id"
)
PAUSED = "SELECT name, kind, reason FROM sinks WHERE state = 'paused' ORDER BY name"

SOURCE_COLUMNS = (
    "status",
    "source_status",
    "amount_minor",
    "authorized_minor",
    "currency",
    "refunded_minor",
    "fee_minor",
    "fee_tax_minor",
    "net_minor",
    "settlement_minor",
    "settlement_currency",
    "method",
    "metadata",
    "customer_email",
    "customer_name",
    "description",
    "source_created_at",
)
"""The business columns a fetch decides: every hashed column but the dispute status, the
not-found mark and the unknown columns."""


@dataclass
class OnPurpose:
    """What a test broke on purpose: the invariants accept these, and only these."""

    conflicts: set[str] = field(default_factory=set[str])
    """Event ids the test sent again with a different event (invariant 7)."""
    dead: set[str] = field(default_factory=set[str])
    """Payment ids given a permanent error, whose jobs may end dead (invariant 8)."""
    alerts: set[str] = field(default_factory=set[str])
    """Alert kinds the test expects to fire (invariant 9)."""
    paused: set[str] = field(default_factory=set[str])
    """Sources and sinks, by name, the test paused; their waiting jobs are held (settle, 4)."""


async def settle(
    data_dir: Path, *, paused: Collection[str] = (), limit: float = SETTLE_LIMIT
) -> None:
    """Return once no job is waiting or running, but for a waiting job a source or sink paused
    on purpose (`paused`, by name) holds. Fail at once on any other pause, and after `limit`
    real seconds naming the jobs still open."""
    deadline = time.monotonic() + limit
    async with Reader.open(data_dir) as reader:
        while True:
            for pause in await reader.fetch_all(PAUSED):
                if pause["name"] not in paused:
                    raise AssertionError(
                        f"{pause['kind']} {pause['name']} paused, not on purpose: {pause['reason']}"
                    )
            still = [job for job in await reader.fetch_all(OPEN_JOBS) if not _held(job, paused)]
            if not still:
                return
            if time.monotonic() > deadline:
                named = ", ".join(_job(job) for job in still)
                raise AssertionError(f"not settled after {limit} s; jobs still open: {named}")
            await asyncio.sleep(0.01)


class LedgerWatch:
    """Every state each ledger row took while the app ran, by row version: the ledger read
    every few milliseconds through the read-only connection while the watch is open. A row
    written twice between two looks shows only its later state."""

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self.seen: dict[str, dict[int, Row]] = {}
        """Payment id → row version → the row as first seen at that version."""
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        await self.look()
        self._task = asyncio.create_task(self._watch())
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self.look()

    async def look(self) -> None:
        async with Reader.open(self._data_dir) as reader:
            self._record(await reader.fetch_all("SELECT * FROM payments"))

    async def _watch(self) -> None:
        async with Reader.open(self._data_dir) as reader:
            while True:
                self._record(await reader.fetch_all("SELECT * FROM payments"))
                await asyncio.sleep(WATCH_EVERY)

    def _record(self, rows: Iterable[Row]) -> None:
        for row in rows:
            self.seen.setdefault(row["payment_id"], {}).setdefault(row["row_version"], dict(row))


def buyers_tabs(sheets: FakeSheets) -> list[Tab]:
    """The buyer's tabs as they stand: take it before the app first writes the sheet, while
    every tab is the buyer's."""
    return copy.deepcopy(sheets.tabs)


@dataclass(frozen=True)
class Outcome:
    """What a settled run left, as the invariants read it."""

    config: Config
    razorpay: FakeRazorpay
    sheets: FakeSheets
    buyers_tabs: Sequence[Tab]
    """The buyer's tabs before the run."""
    ledger_seen: Mapping[str, Mapping[int, Row]]
    """`LedgerWatch.seen`: every state each ledger row was seen to take."""
    events: Sequence[Row]
    deliveries: Sequence[Row]
    jobs: Sequence[Row]
    payments: Sequence[Row]
    alerts: Sequence[Row]
    payments_tab_id: int | None
    daily_summary_tab_id: int | None


async def observe(
    data_dir: Path,
    config: Config,
    razorpay: FakeRazorpay,
    sheets: FakeSheets,
    *,
    buyers_tabs: Sequence[Tab],
    watch: LedgerWatch,
) -> Outcome:
    """Read what the run left; with the app stopped, or settled."""
    async with Reader.open(data_dir) as reader:
        tabs = await reader.fetch_all(
            "SELECT payments_tab_id, daily_summary_tab_id FROM sheet_state"
        )
        return Outcome(
            config=config,
            razorpay=razorpay,
            sheets=sheets,
            buyers_tabs=buyers_tabs,
            ledger_seen=copy.deepcopy(watch.seen),
            events=await reader.fetch_all("SELECT * FROM events ORDER BY event_id"),
            deliveries=await reader.fetch_all("SELECT * FROM deliveries ORDER BY id"),
            jobs=await reader.fetch_all("SELECT * FROM jobs ORDER BY id"),
            payments=await reader.fetch_all("SELECT * FROM payments ORDER BY payment_id"),
            alerts=await reader.fetch_all("SELECT * FROM alerts ORDER BY kind"),
            payments_tab_id=tabs[0]["payments_tab_id"] if tabs else None,
            daily_summary_tab_id=tabs[0]["daily_summary_tab_id"] if tabs else None,
        )


# -- 1. The ledger equals the source ---------------------------------------------------------


def ledger_equals_source(outcome: Outcome, on_purpose: OnPurpose) -> list[str]:
    """Every payment FakeRazorpay knows has a row equal to its current state; no row exists for
    a payment it doesn't know, except one marked not found."""
    found: list[str] = []
    stored = {row["payment_id"]: row for row in outcome.payments}
    for payment_id in sorted(outcome.razorpay.payments):
        row = stored.get(payment_id)
        expected = _expected(outcome, payment_id, -1)
        if expected is None:
            if row is not None and row["not_found_at"] is None:
                found.append(f"{payment_id} has a ledger row, but Razorpay never attempted it")
        elif row is None:
            found.append(f"{payment_id} has no ledger row")
        else:
            differs = [name for name in SOURCE_COLUMNS if row[name] != expected[name]]
            if differs:
                found.append(
                    f"{payment_id} differs from Razorpay's current state in {', '.join(differs)}"
                )
    for payment_id, row in sorted(stored.items()):
        if payment_id not in outcome.razorpay.payments and row["not_found_at"] is None:
            found.append(f"{payment_id} has a ledger row, but Razorpay doesn't know it")
    return found


def _expected(outcome: Outcome, payment_id: str, state: int) -> dict[str, Any] | None:
    """The business columns of one of the payment's states at Razorpay (-1: its current one),
    or None when that state makes no row."""
    states = outcome.razorpay.states(payment_id)
    if not states:
        return None
    row = payment_row(states[state], metadata_keys=outcome.config.razorpay.metadata_keys)
    return None if row is None else business_columns(row)


# -- 2. The sheet equals the ledger ----------------------------------------------------------


def sheet_equals_ledger(outcome: Outcome, on_purpose: OnPurpose) -> list[str]:
    """Every ledger row appears on the Payments tab exactly once, with the values the flusher
    would write; the Daily summary equals its sums. A row whose currency is unknown is held
    back, as the flusher holds it."""
    if not outcome.payments and outcome.payments_tab_id is None:
        return []
    if outcome.payments_tab_id is None or not outcome.sheets.has_tab(outcome.payments_tab_id):
        return ["the Payments tab was never written"]
    columns = payment_columns(outcome.config)
    key = next(i for i, column in enumerate(columns) if column.key == "payment_id")
    on_sheet: dict[str, list[tuple[int, list[Json]]]] = {}
    for at, cells in _rows(outcome.sheets.tab(outcome.payments_tab_id), len(columns)):
        payment_id = cells[key].get("userEnteredValue", {}).get("stringValue")
        if payment_id:
            on_sheet.setdefault(payment_id, []).append((at, cells))
    found: list[str] = []
    for row in outcome.payments:
        payment_id = row["payment_id"]
        rows = on_sheet.pop(payment_id, [])
        try:
            expected = [column.cell(row) for column in columns]
        except UnknownCurrency:
            continue
        if not rows:
            found.append(f"{payment_id} is not on the sheet")
            continue
        if len(rows) > 1:
            found.append(f"{payment_id} is on rows {', '.join(str(at + 1) for at, _ in rows)}")
        differs = [
            column.header
            for i, column in enumerate(columns)
            if _written(rows[0][1][i]) != _written(expected[i])
        ]
        if differs:
            found.append(f"{payment_id} differs on the sheet in {', '.join(differs)}")
    for payment_id, rows in sorted(on_sheet.items()):
        found.append(f"{payment_id} is on the sheet (row {rows[0][0] + 1}) but not in the ledger")
    return found + _summary_differs(outcome)


def _summary_differs(outcome: Outcome) -> list[str]:
    totals = summarise(outcome.payments, outcome.config.service.timezone)
    wanted = {key: t for key, t in totals.items() if t.payments}
    tab_id = outcome.daily_summary_tab_id
    if not wanted and tab_id is None:
        return []
    if tab_id is None or not outcome.sheets.has_tab(tab_id):
        return ["the Daily summary tab was never written"]
    on_sheet: dict[Any, list[Json]] = {}
    for _, cells in _rows(outcome.sheets.tab(tab_id), len(DAILY_SUMMARY_HEADER)):
        key = read_key([{"effectiveValue": cell.get("userEnteredValue", {})} for cell in cells])
        if key is not None:
            on_sheet.setdefault(key, cells)
    found: list[str] = []
    for key in sorted(set(wanted) | set(on_sheet)):
        expected = summary_cells(key, wanted.get(key, Totals()))
        if expected is None:
            continue
        cells = on_sheet.get(key)
        day, currency, paid_out = key
        named = f"{day.isoformat()} {currency}/{paid_out}"
        if cells is None:
            found.append(f"the Daily summary has no row for {named}")
        elif [_written(c) for c in cells[: len(expected)]] != [_written(c) for c in expected]:
            found.append(f"the Daily summary's {named} differs from its sums")
    return found


def _rows(tab: Tab, width: int) -> list[tuple[int, list[Json]]]:
    """Each row below the header with any cell, and its first `width` cells."""
    return [
        (row, cells)
        for row in range(1, tab.last_used_row() + 1)
        if any(cells := [tab.cell(row, column) for column in range(width)])
    ]


def _written(cell: Json) -> Json:
    """What the flusher writes of a cell: its value and its number format."""
    out: Json = {}
    if value := cell.get("userEnteredValue"):
        out["value"] = value
    if number_format := cell.get("userEnteredFormat", {}).get("numberFormat"):
        out["format"] = number_format
    return out


# -- 3. Every event is stored once -----------------------------------------------------------


def every_event_stored_once(outcome: Outcome, on_purpose: OnPurpose) -> list[str]:
    """No event is stored twice (by its fingerprint, or by its signed body), every delivery
    answered `new` stored its event, and every delivery answered is logged exactly once."""
    found: list[str] = []
    delivered = [e for e in outcome.events if e["origin"] == "delivery"]
    for what, column in (("same fingerprint", "fingerprint"), ("same signed body", "body_sha256")):
        for ids in _grouped(delivered, column):
            found.append(f"events {', '.join(ids)} are one event stored twice ({what})")
    answered = [s for s in outcome.razorpay.sent if s.status_code in (200, 202)]
    stored = {e["event_id"] for e in outcome.events}
    for sent in answered:
        if sent.answer.get("result") == "new" and sent.delivery.event_id not in stored:
            found.append(f"{sent.delivery.event_id} was answered new but is not stored")
    by_answer = Counter(str(s.answer.get("result")) for s in answered)
    by_log = Counter(str(d["result"]) for d in outcome.deliveries)
    if by_answer != by_log:
        found.append(
            f"{_counted(by_answer, 'delivery', 'deliveries')} answered ({_by_result(by_answer)}),"
            f" but {by_log.total()} logged ({_by_result(by_log)})"
        )
    return found


def _grouped(rows: Iterable[Row], column: str) -> list[list[str]]:
    groups: dict[Any, list[str]] = {}
    for row in rows:
        if row[column] is not None:
            groups.setdefault(row[column], []).append(row["event_id"])
    return [sorted(ids) for ids in groups.values() if len(ids) > 1]


def _counted(counts: Counter[str], one: str, many: str) -> str:
    n = counts.total()
    return f"{n} {one if n == 1 else many}"


def _by_result(counts: Counter[str]) -> str:
    return ", ".join(f"{n} {result}" for result, n in sorted(counts.items())) or "none"


# -- 4. Nothing is stuck ---------------------------------------------------------------------


def nothing_stuck(outcome: Outcome, on_purpose: OnPurpose) -> list[str]:
    """Every job is done, or dead or resolved with a reason; a waiting job is accepted only
    while a pause on purpose holds it."""
    found: list[str] = []
    for job in outcome.jobs:
        status = job["status"]
        if status == "done" or _held(job, on_purpose.paused):
            continue
        if (status == "dead" and job["last_error"]) or (
            status == "resolved" and job["resolved_note"]
        ):
            continue
        reason = " with no reason" if status in ("dead", "resolved") else ""
        found.append(f"{_job(job)}{reason}")
    return found


# -- 5. Nothing moves backwards --------------------------------------------------------------


def nothing_moves_backwards(outcome: Outcome, on_purpose: OnPurpose) -> list[str]:
    """No ledger row takes a state of its payment older than one it already held, Razorpay's
    states counted in the order the payment went through them."""
    found: list[str] = []
    for payment_id, versions in sorted(outcome.ledger_seen.items()):
        states = outcome.razorpay.states(payment_id)
        expected = [_expected(outcome, payment_id, i) for i in range(len(states))]
        newest: tuple[int, int] | None = None  # (row version, state index)
        for version, row in sorted(versions.items()):
            if row["not_found_at"] is not None:
                continue
            held = [
                i
                for i, columns in enumerate(expected)
                if columns is not None and all(row[c] == columns[c] for c in SOURCE_COLUMNS)
            ]
            if not held:
                continue
            if newest is not None and max(held) < newest[1]:
                found.append(
                    f"{payment_id} went back: row version {version} holds Razorpay's state"
                    f" {max(held) + 1} of {len(states)}, after row version {newest[0]} held"
                    f" state {newest[1] + 1}"
                )
            if newest is None or max(held) >= newest[1]:
                newest = (version, max(held))
    return found


# -- 6. Nothing of the buyer's is touched ----------------------------------------------------


def buyers_cells_untouched(outcome: Outcome, on_purpose: OnPurpose) -> list[str]:
    """Every tab the buyer had before the run is still there, with its title and every cell."""
    found: list[str] = []
    for before in outcome.buyers_tabs:
        if not outcome.sheets.has_tab(before.sheet_id):
            found.append(f"the buyer's tab {before.title} was deleted")
            continue
        now = outcome.sheets.tab(before.sheet_id)
        if now.title != before.title:
            found.append(f"the buyer's tab {before.title} was renamed {now.title}")
        changed = sorted(
            (row, column)
            for row, column in set(before.cells) | set(now.cells)
            if before.cell(row, column) != now.cell(row, column)
        )
        if changed:
            cells = ", ".join(_a1(row, column) for row, column in changed)
            found.append(f"the buyer's tab {before.title}: {cells} changed")
    return found


def _a1(row: int, column: int) -> str:
    letters = ""
    column += 1
    while column:
        column, rest = divmod(column - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return f"{letters}{row + 1}"


# -- 7. No false conflict --------------------------------------------------------------------


def no_false_conflict(outcome: Outcome, on_purpose: OnPurpose) -> list[str]:
    """A conflict is recorded only for an event id the test sent a different event under."""
    conflicts = sorted(
        {d["event_id"] for d in outcome.deliveries if d["result"] == "conflict"}
        - on_purpose.conflicts
    )
    return [
        f"{event_id} recorded as a conflict, but no other event was sent under its id on purpose"
        for event_id in conflicts
    ]


# -- 8. No surprise dead job -----------------------------------------------------------------


def no_surprise_dead_job(outcome: Outcome, on_purpose: OnPurpose) -> list[str]:
    """A job ends dead only for a payment the test gave a permanent error."""
    return [
        f"{_job(job)}: {job['last_error']}"
        for job in outcome.jobs
        if job["status"] == "dead" and job["payment_key"] not in on_purpose.dead
    ]


# -- 9. Quiet alerts -------------------------------------------------------------------------


def quiet_alerts(outcome: Outcome, on_purpose: OnPurpose) -> list[str]:
    """At most the alerts the test expects fire."""
    found: list[str] = []
    for alert in outcome.alerts:
        if alert["kind"] in on_purpose.alerts:
            continue
        if alert["last_sent_at"] is None and not alert["suppressed_count"]:
            continue
        suppressed = alert["suppressed_count"]
        more = f" (and {suppressed} more suppressed)" if suppressed else ""
        found.append(f"alert {alert['kind']} fired{more}")
    return found


# -- all nine --------------------------------------------------------------------------------

type Check = Callable[[Outcome, OnPurpose], list[str]]

INVARIANTS: tuple[tuple[str, Check], ...] = (
    ("the ledger equals the source", ledger_equals_source),
    ("the sheet equals the ledger", sheet_equals_ledger),
    ("every event is stored once", every_event_stored_once),
    ("nothing is stuck", nothing_stuck),
    ("nothing moves backwards", nothing_moves_backwards),
    ("nothing of the buyer's is touched", buyers_cells_untouched),
    ("no false conflict", no_false_conflict),
    ("no surprise dead job", no_surprise_dead_job),
    ("quiet alerts", quiet_alerts),
)


def assert_invariants(outcome: Outcome, on_purpose: OnPurpose) -> None:
    """Run all nine checks; fail naming every invariant broken and what each found."""
    broken = [
        f"invariant {number}, {name}: {finding}"
        for number, (name, check) in enumerate(INVARIANTS, start=1)
        for finding in check(outcome, on_purpose)
    ]
    if broken:
        raise AssertionError("\n".join(broken))


def _held(job: Row, paused: Collection[str]) -> bool:
    """A waiting job a pause on purpose holds: its source's, or its sink's."""
    return job["status"] == "waiting" and (job["source"] in paused or job["sink"] in paused)


def _job(job: Row) -> str:
    """`job 3 (mirror sheets, pay_X) waiting`; a ledger job's sink is the ledger itself."""
    stage = job["stage"] if job["sink"] == job["stage"] else f"{job['stage']} {job['sink']}"
    return f"job {job['id']} ({stage}, {job['payment_key']}) {job['status']}"
