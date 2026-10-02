"""The ledger worker: the ledger stage's work on P1's stage runner (ledger-worker.md).

For one claimed ledger job: fetch the payment's current state with no transaction open (the
runner heartbeats meanwhile), map it to its business row, and return the writes, which the
runner commits in one transaction with the job's fenced `complete`. A row is written only when
its `row_hash` changes; a payment not yet attempted gets none.
"""

import asyncio
import hashlib
import json
import logging
import random
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.config import RetryProfile
from webhook_sync.db.schema import events, payments
from webhook_sync.db.writer import Transaction, Writer
from webhook_sync.queue import (
    ClaimedJob,
    Done,
    Failure,
    Pauses,
    StageFailed,
    StageRunner,
    Work,
    enqueue,
)
from webhook_sync.queue.runner import Sleep
from webhook_sync.sources.base import BusinessRow, NotFound, SourceAdapter

log = logging.getLogger("webhook_sync.ledger")

HASHED_COLUMNS = (
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
    "dispute_status",
    "customer_email",
    "customer_name",
    "description",
    "source_created_at",
    "not_found_at",
    "unknown_columns",
)
"""The fixed list `row_hash` covers: every business column of `payments`, never the key, the
bookkeeping columns or `via` (Q124, A3). Changing it changes every stored hash."""

_SWEPT_ORIGINS = frozenset({"sweep", "backfill"})


def row_hash(columns: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of the hashed columns' stored values."""
    canonical = json.dumps(
        {name: columns.get(name) for name in HASHED_COLUMNS},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


LEDGER_CLAIM_LIMIT = 1
"""The ledger worker claims one job at a time (queue.md)."""


def ledger_runner(
    writer: Writer,
    sources: Mapping[str, SourceAdapter],
    *,
    mirror_sinks: Sequence[str],
    claimed_by: str,
    pauses: Pauses,
    retry_profile: RetryProfile,
    rng: random.Random | None = None,
    sleep: Sleep = asyncio.sleep,
) -> StageRunner:
    """The ledger stage (stage and sink `ledger`) on P1's stage runner. Each real change to a
    row enqueues one mirror job per sink in `mirror_sinks`, the configured sinks (A19)."""
    return StageRunner(
        writer,
        stage="ledger",
        sink="ledger",
        limit=LEDGER_CLAIM_LIMIT,
        work=ledger_work(sources, mirror_sinks=mirror_sinks, retry_profile=retry_profile),
        claimed_by=claimed_by,
        pauses=pauses,
        retry_profile=retry_profile,
        rng=rng,
        sleep=sleep,
    )


def ledger_work(
    sources: Mapping[str, SourceAdapter],
    *,
    mirror_sinks: Sequence[str],
    retry_profile: RetryProfile,
) -> Work:
    """The ledger stage's work, fetching each job's payment from its source adapter."""

    async def work(job: ClaimedJob) -> Done:
        source = sources[job.source]
        fetched = await source.fetch(job.payment_key)
        if isinstance(fetched, Failure):
            _log_fetch(job, "failed", fetched.error_class)
            raise StageFailed(fetched)
        if isinstance(fetched, NotFound):
            _log_fetch(job, "not_found")
            row: BusinessRow | NotFound | None = fetched
        else:
            _log_fetch(job, "fetched")
            row = source.to_row(fetched.state)

        async def writes(tx: Transaction) -> None:
            await write_row(
                tx, job, row, mirror_sinks=mirror_sinks, retry_profile=retry_profile, now=utc_now()
            )

        return Done(writes=writes)

    return work


NOT_FOUND_WITHOUT_ROW = "not found at source: possible mode or account mix-up"
"""Why a job ends dead when the source doesn't know a payment the ledger has no row for (A5)."""


async def write_row(
    tx: Transaction,
    job: ClaimedJob,
    row: BusinessRow | NotFound | None,
    *,
    mirror_sinks: Sequence[str],
    retry_profile: RetryProfile,
    now: datetime,
) -> None:
    """Upsert the payment's business row if its hash changed, and enqueue one mirror job per
    sink, all in the runner's transaction with the job's fenced `complete` (A2, A19).

    `NotFound`: the stored row keeps its values and gets `not_found_at` (a real change the
    first time); with no stored row the job fails dead (A5). A fetched row always clears
    `not_found_at`: the payment is back (A17)."""
    if row is None:
        return  # not yet attempted: no row (F10)
    stored = await _stored_row(tx, job)
    if isinstance(row, NotFound):
        if stored is None:
            raise StageFailed(Failure(NOT_FOUND_WITHOUT_ROW, "dead", http_status=row.http_status))
        columns = {name: stored[name] for name in HASHED_COLUMNS}
        columns["not_found_at"] = stored["not_found_at"] or to_db_text(now)
    else:
        columns = _business_columns(row)
        columns["dispute_status"] = stored["dispute_status"] if stored else None
    new_hash = row_hash(columns)
    if stored is not None and stored["row_hash"] == new_hash:
        return  # nothing changed: nothing written
    via = await _via(tx, job)
    values = {
        **columns,
        "source": job.source,
        "account": job.account,
        "payment_id": job.payment_key,
        "row_hash": new_hash,
        "row_version": (stored["row_version"] if stored else 0) + 1,
        "last_event_id": job.caused_by_event_id or (stored["last_event_id"] if stored else None),
        "via": via or (stored["via"] if stored else None),
        "updated_at": to_db_text(now),
    }
    insert = sqlite.insert(payments).values(values)
    await tx.execute(
        insert.on_conflict_do_update(
            index_elements=[payments.c.source, payments.c.account, payments.c.payment_id],
            set_={name: insert.excluded[name] for name in values},
        )
    )
    for sink in mirror_sinks:
        await enqueue(
            tx,
            stage="mirror",
            sink=sink,
            source=job.source,
            account=job.account,
            payment_key=job.payment_key,
            event_id=job.caused_by_event_id,
            retry_profile=retry_profile,
            now=now,
        )


async def _stored_row(tx: Transaction, job: ClaimedJob) -> Mapping[str, Any] | None:
    rows = await tx.execute(
        sa.select(payments).where(
            payments.c.source == job.source,
            payments.c.account == job.account,
            payments.c.payment_id == job.payment_key,
        )
    )
    return dict(rows[0]) if rows else None


def _business_columns(row: BusinessRow) -> dict[str, Any]:
    """The row's business columns as stored: times as text, metadata as canonical JSON."""
    return {
        "status": row.status,
        "source_status": row.source_status,
        "amount_minor": row.amount_minor,
        "authorized_minor": row.authorized_minor,
        "currency": row.currency,
        "refunded_minor": row.refunded_minor,
        "fee_minor": row.fee_minor,
        "fee_tax_minor": row.fee_tax_minor,
        "net_minor": row.net_minor,
        "settlement_minor": row.settlement_minor,
        "settlement_currency": row.settlement_currency,
        "method": row.method,
        "metadata": (
            None
            if row.metadata is None
            else json.dumps(dict(row.metadata), sort_keys=True, ensure_ascii=False)
        ),
        "customer_email": row.customer_email,
        "customer_name": row.customer_name,
        "description": row.description,
        "source_created_at": (
            None if row.source_created_at is None else to_db_text(row.source_created_at)
        ),
        "not_found_at": None,
        "unknown_columns": None,
    }


async def _via(tx: Transaction, job: ClaimedJob) -> str | None:
    """How the job's causing event reached us: `sweep` for a sweep or backfill, otherwise
    `delivery`; None when the job names no event, so the stored `via` stays (A13)."""
    if job.caused_by_event_id is None:
        return None
    rows = await tx.execute(
        sa.select(events.c.origin).where(
            events.c.source == job.source, events.c.event_id == job.caused_by_event_id
        )
    )
    if not rows:
        return None
    return "sweep" if rows[0]["origin"] in _SWEPT_ORIGINS else "delivery"


def _log_fetch(job: ClaimedJob, outcome: str, error_class: str | None = None) -> None:
    """One line per fetch outcome: never a key, a body, an email or a phone number."""
    log.info(
        "payment fetch",
        extra={
            "job_id": job.id,
            "payment_key": job.payment_key,
            "source": job.source,
            "outcome": outcome,
            "error_class": error_class,
        },
    )
