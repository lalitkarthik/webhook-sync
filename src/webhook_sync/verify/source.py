"""Verify, Razorpay against ledger (verify.md, Q151): the source half of the nightly full run.

Razorpay's payments list gives every payment created in the last `[verify] days`, and its refunds
list every refund created in that window and the 21 days before it. A listed payment's refunds
come back empty (G71), so its processed refunds are attached from the refunds list. Each listed
payment is mapped with P2's mapping and compared with its `payments` row over the business
columns, the card network excluded: a list can't expand the card. What disagrees is a finding:

- `missing`: listed, with no row; `stale`: a row whose business columns differ; both repaired by
  a ledger job, which fetches the payment again;
- `orphaned`: a row created in the window that Razorpay doesn't list; reported only, never
  changed: usually deleted test data or the wrong account;
- `not_found` (the row the ledger worker flagged not found at source, rather than `orphaned`)
  and `unknown_columns`: listed so a person knows.

Work in progress is skipped as in the sheet check: a payment with a live job, changed since the
run started, or under an hour old with the fee pending. So is a payment created at or after the
sweeper's payments checkpoint while the sweeper keeps up (it moved the checkpoint within the last
two sweep periods): the next sweep brings it in, with Via = sweep. A sweeper that stalled, or
never swept, is not trusted: everything is compared, and what it missed is `missing`.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa

from webhook_sync.clock import to_db_text
from webhook_sync.config import Config
from webhook_sync.db.schema import checkpoints, events, payments
from webhook_sync.db.writer import Writer
from webhook_sync.ledger.worker import HASHED_COLUMNS, business_columns
from webhook_sync.queue import Failure, Pauses
from webhook_sync.sources.base import UnusableState
from webhook_sync.sources.razorpay import RazorpayAdapter
from webhook_sync.sweeper.sweep import SWEEP_EVERY
from webhook_sync.verify.runs import Finding, in_progress, live_payments

log = logging.getLogger("webhook_sync.verify")

SWEEPER_FRESH = 2 * SWEEP_EVERY
"""A sweeper that moved its checkpoint within this long is keeping up."""

REFUNDS_BEFORE = timedelta(days=21)
"""How much further back than the payments the refunds list is read (Q151)."""

COMPARED = tuple(
    name for name in HASHED_COLUMNS if name not in ("dispute_status", "unknown_columns")
)
"""The business columns a listed payment is compared over: not the dispute status, which no
list carries, nor the unknown columns, listed on their own. A row flagged not found that
Razorpay lists is stale: a fetch clears the flag."""


@dataclass(frozen=True)
class SourceCheck:
    """What one check against Razorpay found: the payments compared, the differences and the
    events ignored in the window."""

    checked: int
    findings: list[Finding]
    ignored_events: int


class RazorpayVerify:
    """Razorpay against the ledger, for the configured account."""

    def __init__(
        self, writer: Writer, razorpay: RazorpayAdapter, config: Config, *, pauses: Pauses
    ) -> None:
        self._writer = writer
        self._razorpay = razorpay
        self._config = config
        self._pauses = pauses

    async def check(self, started: datetime) -> SourceCheck | Failure:
        """Every payment created in the window before `started` against its row; a list's
        failure as it came."""
        window = timedelta(days=self._config.verify.days)
        end = _seconds_of(started)
        start = end - _seconds(window)
        listed = await self._razorpay.list_payments(
            start, end, writer=self._writer, pauses=self._pauses
        )
        if isinstance(listed, Failure):
            return listed
        refunds = await self._razorpay.list_refunds(
            start - _seconds(REFUNDS_BEFORE), end, writer=self._writer, pauses=self._pauses
        )
        if isinstance(refunds, Failure):
            return refunds
        processed: dict[str, list[dict[str, Any]]] = {}
        for refund in refunds.items:
            if refund.get("status") == "processed":
                processed.setdefault(refund["payment_id"], []).append(refund)
        unswept = await self._unswept_from(started)
        if unswept is None:
            unswept = end + 1  # nothing is the sweeper's
        ledger = await self._ledger()
        live = {key[2] for key in await live_payments(self._writer) if key[:2] == self._key}
        skipped = {
            payment_id
            for payment_id, row in ledger.items()
            if payment_id in live or in_progress(row, started)
        }

        findings: list[Finding] = []
        compared: set[str] = set()
        for item in listed.items:
            payment_id: str = item["id"]
            if item["created_at"] >= unswept:
                continue  # the sweeper's to bring in
            state = {
                **item,
                "refunds": {"entity": "collection", "items": processed.get(payment_id, [])},
            }
            try:
                row = self._razorpay.to_row(state)
            except UnusableState as error:
                log.warning(
                    "verify: a listed payment can't be mapped",
                    extra={"payment_key": payment_id, "error": str(error)},
                )
                continue
            if row is None or payment_id in live or payment_id in skipped:
                continue  # not attempted yet, or work in progress
            compared.add(payment_id)
            stored = ledger.get(payment_id)
            if stored is None:
                findings.append(Finding("missing", payment_id, repair=payment_id, stage="ledger"))
                continue
            differs = _differs(business_columns(row), stored)
            if differs:
                findings.append(
                    Finding(
                        "stale", payment_id, ", ".join(differs), repair=payment_id, stage="ledger"
                    )
                )

        listed_ids = {item["id"] for item in listed.items}
        since = to_db_text(started - window)
        for payment_id, stored in sorted(ledger.items()):
            created = stored["source_created_at"]
            if payment_id in skipped or created is None or created < since:
                continue
            if payment_id not in listed_ids:
                compared.add(payment_id)
                kind = "not_found" if stored["not_found_at"] is not None else "orphaned"
                findings.append(Finding(kind, payment_id))
            if stored["unknown_columns"] is not None:
                findings.append(Finding("unknown_columns", payment_id, stored["unknown_columns"]))
        return SourceCheck(len(compared), findings, await self._ignored(since))

    async def _unswept_from(self, started: datetime) -> int | None:
        """Where the sweeper's payments are still to come, in Unix seconds: its payments
        checkpoint, if it moved within `SWEEPER_FRESH` of `started`; else None."""
        source, account = self._key
        async with self._writer.transaction() as tx:
            rows = await tx.execute(
                sa.select(checkpoints.c.checkpoint_at, checkpoints.c.last_sweep_at).where(
                    checkpoints.c.source == source,
                    checkpoints.c.account == account,
                    checkpoints.c.list == "payments",
                )
            )
        swept = rows[0]["last_sweep_at"] if rows else None
        if swept is None or swept < to_db_text(started - SWEEPER_FRESH):
            return None
        return _seconds_of(datetime.fromisoformat(rows[0]["checkpoint_at"]))

    @property
    def _key(self) -> tuple[str, str]:
        return self._razorpay.name, self._config.razorpay.account

    async def _ledger(self) -> dict[str, Mapping[str, Any]]:
        """The account's payments, by payment id."""
        source, account = self._key
        async with self._writer.transaction() as tx:
            rows = await tx.execute(
                sa.select(payments).where(
                    payments.c.source == source, payments.c.account == account
                )
            )
        return {r["payment_id"]: dict(r) for r in rows}

    async def _ignored(self, since: str) -> int:
        """The account's events ignored since `since`: payments the proof doesn't cover (F38)."""
        source, account = self._key
        async with self._writer.transaction() as tx:
            [row] = await tx.execute(
                sa.select(sa.func.count().label("n")).where(
                    events.c.source == source,
                    events.c.account == account,
                    events.c.ignore_reason.is_not(None),
                    events.c.received_at >= since,
                )
            )
        return row["n"]


def _differs(expected: Mapping[str, Any], stored: Mapping[str, Any]) -> list[str]:
    """The compared columns whose stored value isn't what Razorpay's listed state maps to."""
    return [
        name
        for name in COMPARED
        if _comparable(name, expected[name]) != _comparable(name, stored[name])
    ]


def _comparable(name: str, value: Any) -> Any:
    """A method without its card network, which only a fetch can expand: `card (Visa)` is
    `card`."""
    if name == "method" and isinstance(value, str) and value.startswith("card ("):
        return "card"
    return value


def _seconds_of(moment: datetime) -> int:
    return int(moment.timestamp())


def _seconds(span: timedelta) -> int:
    return int(span.total_seconds())
