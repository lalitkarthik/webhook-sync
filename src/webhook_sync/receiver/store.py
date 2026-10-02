"""The one transaction: store a verified delivery's event if absent, and always its delivery row.

receiver.md, "The one transaction". A new handled event is stored with its ledger job (the
outbox); a new event (handled or ignored) is answered 202. An event already stored makes no job
and is answered 200: a duplicate when the signed body's hash matches or the fingerprint is the
same, a conflict when a known event id arrives with a different fingerprint (the first is kept).
"""

import json
import time
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from webhook_sync.clock import to_db_text
from webhook_sync.config import Mode, RetryProfile
from webhook_sync.db.schema import deliveries, events
from webhook_sync.db.writer import Transaction, Writer
from webhook_sync.queue import enqueue
from webhook_sync.receiver.demo import FailFirst
from webhook_sync.sources.base import EventDescription, VerifiedDelivery

type DeliveryResult = Literal["new", "duplicate", "conflict"]


@dataclass(frozen=True)
class StoredDelivery:
    result: DeliveryResult
    status_code: int
    event_id: str
    """The stored event's id: for a body-hash replay, the first event's, not the header's."""
    stored_fingerprint: str | None = None
    """For a conflict, the fingerprint of the event kept."""
    attempt: int | None = None
    """With the demo fail-first switch on, this delivery's attempt number for its event id."""


async def store_delivery(
    writer: Writer,
    *,
    source: str,
    account: str,
    mode: Mode,
    retry_profile: RetryProfile,
    delivery: VerifiedDelivery,
    description: EventDescription,
    received_at: datetime,
    started: float,
    fail_first: FailFirst | None = None,
) -> StoredDelivery:
    """Run the one transaction; `started` is the request's `time.perf_counter()` at arrival.

    With `fail_first` (the demo switch), the event is stored as normal and the attempt is then
    answered, and recorded in `deliveries`, with the switch's status.
    """
    async with writer.transaction() as tx:
        inserted = await tx.execute(
            sqlite.insert(events)
            .values(
                source=source,
                event_id=delivery.event_id,
                account=account,
                type=description.type,
                mode=mode,
                fingerprint=description.fingerprint,
                body_sha256=delivery.body_sha256,
                payment_key=description.payment_key,
                origin="delivery",
                outcome="queued" if description.handled else "ignored",
                ignore_reason=description.ignore_reason,
                payload=json.dumps(description.payload, ensure_ascii=False),
                source_created_at=(
                    to_db_text(description.source_created_at)
                    if description.source_created_at
                    else None
                ),
                received_at=to_db_text(received_at),
            )
            .on_conflict_do_nothing()
            .returning(events.c.event_id)
        )
        if inserted:
            if description.handled:
                await _enqueue_ledger_job(
                    tx,
                    source=source,
                    account=account,
                    event_id=delivery.event_id,
                    payment_key=description.payment_key,
                    retry_profile=retry_profile,
                    now=received_at,
                )
            stored = StoredDelivery("new", 202, delivery.event_id)
        else:
            stored = await _repeat(tx, source, delivery, description.fingerprint)
        if fail_first is not None:
            attempt = fail_first.attempt(source, stored.event_id)
            stored = replace(
                stored, status_code=fail_first.answer(attempt, stored.status_code), attempt=attempt
            )
        await tx.execute(
            sa.insert(deliveries).values(
                source=source,
                event_id=stored.event_id,
                result=stored.result,
                status_code=stored.status_code,
                received_at=to_db_text(received_at),
                duration_ms=round((time.perf_counter() - started) * 1000),
            )
        )
    return stored


async def _enqueue_ledger_job(
    tx: Transaction,
    *,
    source: str,
    account: str,
    event_id: str,
    payment_key: str | None,
    retry_profile: RetryProfile,
    now: datetime,
) -> None:
    """Enqueue the payment's ledger job and point the event at it (`events.job_id`, F17)."""
    if payment_key is None:  # the adapter rejects a handled event that names no payment
        raise ValueError(f"handled event {event_id} names no payment")
    job_id = await enqueue(
        tx,
        stage="ledger",
        sink="ledger",
        source=source,
        account=account,
        payment_key=payment_key,
        event_id=event_id,
        retry_profile=retry_profile,
        now=now,
    )
    await tx.execute(
        sa.update(events)
        .where(events.c.source == source, events.c.event_id == event_id)
        .values(job_id=job_id)
    )


async def _repeat(
    tx: Transaction, source: str, delivery: VerifiedDelivery, fingerprint: str
) -> StoredDelivery:
    """The stored event this delivery collided with, by event id first, else by body hash.

    A body-hash match under another id is a duplicate (A17); under the same id, a different
    fingerprint is a conflict (Q71).
    """
    rows = await tx.execute(
        sa.select(events.c.event_id, events.c.fingerprint)
        .where(
            events.c.source == source,
            sa.or_(
                events.c.event_id == delivery.event_id,
                events.c.body_sha256 == delivery.body_sha256,
            ),
        )
        .order_by((events.c.event_id == delivery.event_id).desc())
        .limit(1)
    )
    event_id, stored_fingerprint = rows[0]["event_id"], rows[0]["fingerprint"]
    if event_id == delivery.event_id and stored_fingerprint != fingerprint:
        return StoredDelivery("conflict", 200, event_id, stored_fingerprint)
    return StoredDelivery("duplicate", 200, event_id)
