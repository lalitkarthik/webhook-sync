"""The Daily summary and Sync status tabs' cells (sheets-sink.md "Daily summary", "Sync status").

Daily summary (Q137, A15): one row per local day, currency and fee currency, from succeeded
payments only (partly and fully refunded included). A payment's day is its `source_created_at`
in `service.timezone`; its refunds count on that day. Payments, Gross and Refunds are in the
currency paid in; Fees and Net (before refunds) in the fee currency, and only from payments
whose fee is known: a fee-pending payment counts in Payments, Gross, Refunds and Fee pending.
Each touched day is recomputed from the ledger, never incremented, so a flush done twice
writes the same numbers.

Sync status (Q138): label/value rows, rewritten whole on every flush.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from webhook_sync.clock import to_db_text
from webhook_sync.sinks.encoding import UnknownCurrency, amount, moment, text

type Json = dict[str, Any]
type SummaryKey = tuple[date, str, str]
"""A Daily summary row's key: local day, currency, fee currency."""

SUCCEEDED = ("succeeded", "partially_refunded", "refunded")
"""The ledger statuses the Daily summary counts."""

KEY_COLUMNS = 3
DAY_PATTERN = "yyyy-mm-dd"
_SERIAL_EPOCH = date(1899, 12, 30)

LAST_VERIFY = "Last verify"


@dataclass
class Totals:
    payments: int = 0
    gross: int = 0
    refunds: int = 0
    fees: int = 0
    net: int = 0
    fee_pending: int = 0
    source: str | None = None


def day_of(stored: str, zone: ZoneInfo) -> date:
    """The local day of a stored UTC time."""
    return datetime.fromisoformat(stored).astimezone(zone).date()


def day_bounds(day: date, zone: ZoneInfo) -> tuple[str, str]:
    """The stored-time range [start, end) of a local day, comparable as text."""
    start = datetime.combine(day, time(), tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(), tzinfo=zone).astimezone(UTC)
    return to_db_text(start), to_db_text(end)


def fee_currency(row: Mapping[str, Any]) -> str:
    return row["settlement_currency"] or row["currency"]


def summarise(rows: Iterable[Mapping[str, Any]], zone: ZoneInfo) -> dict[SummaryKey, Totals]:
    """Each key's totals over `rows`; rows that are not succeeded, or have no time, are left
    out."""
    totals: dict[SummaryKey, Totals] = {}
    for row in rows:
        if row["status"] not in SUCCEEDED or not row["source_created_at"]:
            continue
        key = (day_of(row["source_created_at"], zone), row["currency"], fee_currency(row))
        found = totals.setdefault(key, Totals(source=row["source"]))
        found.payments += 1
        found.gross += row["amount_minor"]
        found.refunds += row["refunded_minor"]
        if row["fee_minor"] is None:
            found.fee_pending += 1
        else:
            found.fees += row["fee_minor"]
            found.net += row["net_minor"] or 0
    return totals


def key_cells(key: SummaryKey) -> list[Json]:
    day, currency, paid_out = key
    return [day_cell(day), text(currency), text(paid_out)]


def total_cells(key: SummaryKey, totals: Totals) -> list[Json]:
    """Payments, Gross, Refunds, Fees, Net (before refunds), Fee pending. Raises
    `UnknownCurrency` for a currency in no exponent table."""
    _, currency, paid_out = key
    return [
        count(totals.payments),
        amount(totals.gross, currency, totals.source),
        amount(totals.refunds, currency, totals.source),
        amount(totals.fees, paid_out, totals.source),
        amount(totals.net, paid_out, totals.source),
        count(totals.fee_pending),
    ]


def summary_cells(key: SummaryKey, totals: Totals) -> list[Json] | None:
    """A whole row, or None when its currency is unknown (held back like its payments)."""
    try:
        return [*key_cells(key), *total_cells(key, totals)]
    except UnknownCurrency:
        return None


def day_cell(day: date) -> Json:
    return {
        "userEnteredValue": {"numberValue": (day - _SERIAL_EPOCH).days},
        "userEnteredFormat": {"numberFormat": {"type": "DATE", "pattern": DAY_PATTERN}},
    }


def count(n: int) -> Json:
    return {
        "userEnteredValue": {"numberValue": n},
        "userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0"}},
    }


def read_key(values: list[Json]) -> SummaryKey | None:
    """A Daily summary row's key from its first three cells' effective values, or None."""
    found: list[Any] = [
        (values[i] if i < len(values) else {}).get("effectiveValue", {}) for i in range(3)
    ]
    day_value: Json = found[0]
    day: date | None = None
    if "numberValue" in day_value:
        day = _SERIAL_EPOCH + timedelta(days=int(day_value["numberValue"]))
    elif "stringValue" in day_value:
        try:
            day = date.fromisoformat(day_value["stringValue"])
        except ValueError:
            return None
    currency = found[1].get("stringValue")
    paid_out = found[2].get("stringValue")
    if day is None or not isinstance(currency, str) or not isinstance(paid_out, str):
        return None
    return day, currency, paid_out


@dataclass(frozen=True)
class SyncStatus:
    last_flush: datetime
    rows_written_last: int
    rows_written_total: int
    jobs_waiting: int
    dead_jobs: int
    sheet_state: str
    razorpay_state: str
    deployment_id: str | None
    last_verify: str | None = None
    """The latest verify run's line (A17), blank until one has run."""


def sync_status_rows(status: SyncStatus, zone: ZoneInfo) -> list[list[Json]]:
    """The label/value rows below the header, in order."""
    lines: list[tuple[str, Json]] = [
        ("Last flush", moment(to_db_text(status.last_flush), zone)),
        ("Rows written (last flush)", count(status.rows_written_last)),
        ("Rows written (total)", count(status.rows_written_total)),
        ("Sheet jobs waiting", count(status.jobs_waiting)),
        ("Dead jobs", count(status.dead_jobs)),
        ("Sheet state", text(status.sheet_state)),
        ("Razorpay state", text(status.razorpay_state)),
        (LAST_VERIFY, text(status.last_verify)),
        ("Deployment id", text(status.deployment_id)),
    ]
    return [[text(label), value] for label, value in lines]


def state_text(state: str | None, reason: str | None) -> str:
    """A `sinks` row as Sync status shows it: `active`, or `paused: <reason>`."""
    if state == "paused":
        return f"paused: {reason}" if reason else "paused"
    return "active"
