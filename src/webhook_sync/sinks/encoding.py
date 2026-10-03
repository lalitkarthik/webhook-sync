"""The Payments tab's columns and how each one's cell is encoded (sheets-sink.md "Encoding").

Cells are typed `updateCells` values, never user-entered, so nothing is re-parsed: text as
`stringValue`, amounts and dates as `numberValue` with an explicit number format. Dates are
serials in `service.timezone` with the pattern `yyyy-mm-dd hh:mm:ss`; amounts are two-decimal
INR (version 1, A21). Fee, GST on fee and Net say `pending` while the fee is unknown.

A column is a key from `[sinks.sheets] columns`, its fixed header and its cell; `metadata`
expands to one column per `[sources.razorpay] metadata_keys` entry.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from webhook_sync.config import Config

type Json = dict[str, Any]
type Row = Mapping[str, Any]

DATE_PATTERN = "yyyy-mm-dd hh:mm:ss"
AMOUNT_PATTERN = "0.00"
PENDING = "pending"
_SERIAL_EPOCH = datetime(1899, 12, 30)
_SECONDS_A_DAY = 86400


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    cell: Callable[[Row], Json]


def payment_columns(config: Config) -> tuple[Column, ...]:
    """The configured Payments columns, in order, `metadata` expanded per key."""
    zone = config.service.timezone
    label = zone_label(zone)
    columns: list[Column] = []
    for key in config.sheets.columns:
        if key == "metadata":
            columns.extend(
                Column(name, name, _metadata(name)) for name in config.razorpay.metadata_keys
            )
            continue
        header, cell = _COLUMNS[key](zone)
        columns.append(Column(key, header.format(zone=label), cell))
    return tuple(columns)


def zone_label(zone: ZoneInfo) -> str:
    """The zone as headers show it: its abbreviation where it has no daylight saving (IST),
    its name where it does (Europe/Berlin)."""
    january = datetime(2026, 1, 1, tzinfo=zone)
    july = datetime(2026, 7, 1, tzinfo=zone)
    if january.utcoffset() != july.utcoffset():
        return zone.key
    return january.tzname() or zone.key


def text(value: object) -> Json:
    if value is None or value == "":
        return {}
    return {"userEnteredValue": {"stringValue": str(value)}}


def amount(minor: int | None) -> Json:
    if minor is None:
        return {}
    return {
        "userEnteredValue": {"numberValue": float(Decimal(minor) / 100)},
        "userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": AMOUNT_PATTERN}},
    }


def moment(stored: str | None, zone: ZoneInfo) -> Json:
    """A stored UTC time as a date serial in `zone`, with the explicit pattern."""
    if stored is None:
        return {}
    local = datetime.fromisoformat(stored).astimezone(zone).replace(tzinfo=None)
    serial = (local - _SERIAL_EPOCH).total_seconds() / _SECONDS_A_DAY
    return {
        "userEnteredValue": {"numberValue": serial},
        "userEnteredFormat": {"numberFormat": {"type": "DATE_TIME", "pattern": DATE_PATTERN}},
    }


def _fee(name: str) -> Callable[[Row], Json]:
    def cell(row: Row) -> Json:
        return text(PENDING) if row["fee_minor"] is None else amount(row[name])

    return cell


def _metadata(name: str) -> Callable[[Row], Json]:
    def cell(row: Row) -> Json:
        stored = row["metadata"]
        values: dict[str, Any] = json.loads(stored) if stored else {}
        return text(values.get(name))

    return cell


def _named(
    header: str, cell: Callable[[Row], Json]
) -> Callable[[ZoneInfo], tuple[str, Callable[[Row], Json]]]:
    return lambda zone: (header, cell)


def _timed(header: str, name: str) -> Callable[[ZoneInfo], tuple[str, Callable[[Row], Json]]]:
    return lambda zone: (header, lambda row: moment(row[name], zone))


_COLUMNS: dict[str, Callable[[ZoneInfo], tuple[str, Callable[[Row], Json]]]] = {
    "payment_id": _named("Payment ID", lambda row: text(row["payment_id"])),
    "source": _named("Source", lambda row: text(row["source"])),
    "created": _timed("Created ({zone})", "source_created_at"),
    "status": _named("Status", lambda row: text(row["status"])),
    "amount": _named("Amount", lambda row: amount(row["amount_minor"])),
    "currency": _named("Currency", lambda row: text(row["currency"])),
    "refunded": _named("Refunded", lambda row: amount(row["refunded_minor"])),
    "fee": _named("Fee", _fee("fee_minor")),
    "gst_on_fee": _named("GST on fee", _fee("fee_tax_minor")),
    "net": _named("Net", _fee("net_minor")),
    "fee_currency": _named(
        "Fee currency", lambda row: text(row["settlement_currency"] or row["currency"])
    ),
    "method": _named("Method", lambda row: text(row["method"])),
    "customer_email": _named("Customer email", lambda row: text(row["customer_email"])),
    "description": _named("Description", lambda row: text(row["description"])),
    "via": _named("Via", lambda row: text(row["via"])),
    "note": _named("Note", lambda row: {}),
    "updated": _timed("Updated ({zone})", "updated_at"),
}
"""Every column key but `metadata`: its header (`{zone}` filled in) and its cell. Masking,
the Note's wording, other currencies' exponents and the cell-size cut come later."""
