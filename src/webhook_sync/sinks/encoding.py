"""The Payments tab's columns and how each one's cell is encoded (sheets-sink.md "Encoding").

Cells are typed `updateCells` values, never user-entered, so nothing is re-parsed: text as
`stringValue`, amounts and dates as `numberValue` with an explicit number format. Dates are
serials in `service.timezone` with the pattern `yyyy-mm-dd hh:mm:ss`; amounts are
`Decimal(minor) / 10^exponent` with a number format per exponent, as text past 15 significant
digits; an unknown currency raises `UnknownCurrency`, so the flusher holds that row back. Fee,
GST on fee and Net say `pending` while the fee is unknown. Text is cut at 50,000 UTF-16 code
units (Q142); emails are masked when `mask_email` (A16); the Note explains odd rows.

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
from webhook_sync.sinks.currencies import EXPONENTS, SOURCE_OVERRIDES

type Json = dict[str, Any]
type Row = Mapping[str, Any]

DATE_PATTERN = "yyyy-mm-dd hh:mm:ss"
PENDING = "pending"
CELL_LIMIT = 50_000
"""Sheets' hard limit per cell, in UTF-16 code units: an emoji counts twice (G69, Q142)."""
TRUNCATED = "…[truncated]"
EXACT_DIGITS = 15
"""Significant digits a Sheets number (a double) holds exactly; past them an amount is text."""
MASK = "***"
NOT_FOUND = "not found at source"
_SERIAL_EPOCH = datetime(1899, 12, 30)
_SECONDS_A_DAY = 86400


class UnknownCurrency(Exception):
    """A currency code in no exponent table: its row is held back, never written wrong (Q62)."""

    def __init__(self, code: str) -> None:
        super().__init__(f"unknown currency {code!r}")
        self.code = code


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
        if key == "customer_email" and config.sheets.mask_email:
            cell = _masked_email
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
    """Text as a `stringValue`, stored as given (`=…`, `+91…`, `007`), cut to fit a cell."""
    if value is None or value == "":
        return {}
    return {"userEnteredValue": {"stringValue": cut(str(value))}}


def cut(value: str) -> str:
    """`value` within `CELL_LIMIT` UTF-16 code units: past it, cut and ending `TRUNCATED`,
    never splitting a surrogate pair."""
    units = value.encode("utf-16-le")
    if len(units) // 2 <= CELL_LIMIT:
        return value
    keep = units[: (CELL_LIMIT - len(TRUNCATED.encode("utf-16-le")) // 2) * 2]
    if 0xD800 <= int.from_bytes(keep[-2:], "little") <= 0xDBFF:
        keep = keep[:-2]  # a high surrogate whose pair was cut off
    return keep.decode("utf-16-le") + TRUNCATED


def exponent(currency: str, source: str | None = None) -> int:
    """How many decimals `currency` has: the source's override, else the table; codes
    upper-cased. Raises `UnknownCurrency` for a code in neither."""
    code = currency.upper()
    found = SOURCE_OVERRIDES.get(source or "", {}).get(code, EXPONENTS.get(code))
    if found is None:
        raise UnknownCurrency(code)
    return found


def amount(minor: int | None, currency: str, source: str | None = None) -> Json:
    """`Decimal(minor) / 10^exponent` as a number with a format per exponent, or as text past
    `EXACT_DIGITS` significant digits."""
    if minor is None:
        return {}
    places = exponent(currency, source)
    value = Decimal(minor).scaleb(-places)
    if len(Decimal(minor).normalize().as_tuple().digits) > EXACT_DIGITS:
        return {"userEnteredValue": {"stringValue": f"{value:f}"}}
    pattern = "0" if places == 0 else "0." + "0" * places
    return {
        "userEnteredValue": {"numberValue": float(value)},
        "userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": pattern}},
    }


def mask_email(email: str) -> str:
    """First character + `***` + domain (A16); `***` when there is no `@`."""
    local, at, domain = email.rpartition("@")
    if not at:
        return MASK
    return f"{local[:1]}{MASK}@{domain}"


def note(row: Row) -> str:
    """The Note: "not found at source" and/or "unknown: <columns>", joined by `; `."""
    notes: list[str] = []
    if row["not_found_at"]:
        notes.append(NOT_FOUND)
    unknown = row["unknown_columns"]
    if unknown:
        names: list[str] = json.loads(unknown) if unknown.startswith("[") else unknown.split(",")
        notes.append("unknown: " + ", ".join(name.strip() for name in names))
    return "; ".join(notes)


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


def _fee_currency(row: Row) -> str:
    return row["settlement_currency"] or row["currency"]


def _paid(name: str) -> Callable[[Row], Json]:
    """An amount in the payment's currency."""
    return lambda row: amount(row[name], row["currency"], row["source"])


def _fee(name: str) -> Callable[[Row], Json]:
    """An amount in the fee currency, `pending` while the fee is unknown."""

    def cell(row: Row) -> Json:
        if row["fee_minor"] is None:
            return text(PENDING)
        return amount(row[name], _fee_currency(row), row["source"])

    return cell


def _masked_email(row: Row) -> Json:
    email = row["customer_email"]
    return text(mask_email(email) if email else None)


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
    "amount": _named("Amount", _paid("amount_minor")),
    "currency": _named("Currency", lambda row: text(row["currency"])),
    "refunded": _named("Refunded", _paid("refunded_minor")),
    "fee": _named("Fee", _fee("fee_minor")),
    "gst_on_fee": _named("GST on fee", _fee("fee_tax_minor")),
    "net": _named("Net", _fee("net_minor")),
    "fee_currency": _named("Fee currency", lambda row: text(_fee_currency(row))),
    "method": _named("Method", lambda row: text(row["method"])),
    "customer_email": _named("Customer email", lambda row: text(row["customer_email"])),
    "description": _named("Description", lambda row: text(row["description"])),
    "via": _named("Via", lambda row: text(row["via"])),
    "note": _named("Note", lambda row: text(note(row))),
    "updated": _timed("Updated ({zone})", "updated_at"),
}
"""Every column key but `metadata`: its header (`{zone}` filled in) and its cell, the customer
email unmasked (`payment_columns` masks it when `mask_email`)."""
