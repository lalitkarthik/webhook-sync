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

type Origin = Literal["delivery", "sweep", "backfill", "replay"]
"""How an event reached us (CONTEXT.md, Origin)."""


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
    job_id: int | None = None
    """For a new handled event, the ledger job it was stored with or folded into."""


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
        new = await store_event(
            tx,
            source=source,
            account=account,
            mode=mode,
            retry_profile=retry_profile,
            event_id=delivery.event_id,
            description=description,
            origin="delivery",
            body_sha256=delivery.body_sha256,
            received_at=received_at,
        )
        if new is not None:
            stored = StoredDelivery("new", 202, delivery.event_id, job_id=new.job_id)
        else:
            stored = await _repeat(tx, source, delivery, description.fingerprint)
        if fail_first is not None:
            attempt = fail_first.next_attempt(source, stored.event_id)
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
    if fail_first is not None:  # committed: a rolled-back attempt is not counted
        fail_first.record(source, stored.event_id)
    return stored


@dataclass(frozen=True)
class NewEvent:
    """An event `store_event` stored: with its ledger job when its type is handled."""

    job_id: int | None


async def store_event(
    tx: Transaction,
    *,
    source: str,
    account: str,
    mode: Mode,
    retry_profile: RetryProfile,
    event_id: str,
    description: EventDescription,
    origin: Origin,
    body_sha256: str | None,
    received_at: datetime,
) -> NewEvent | None:
    """Insert the event if absent; a new handled event gets its ledger job (the outbox). None
    when the event id is already stored: nothing is written.

    The receiver stores deliveries through it and the sweeper its synthetic events, so both
    fill `events` the same way (A15). A synthetic event has no `body_sha256` and no delivery row.
    """
    inserted = await tx.execute(
        sqlite.insert(events)
        .values(
            source=source,
            event_id=event_id,
            account=account,
            type=description.type,
            mode=mode,
            fingerprint=description.fingerprint,
            body_sha256=body_sha256,
            payment_key=description.payment_key,
            origin=origin,
            outcome="queued" if description.handled else "ignored",
            ignore_reason=description.ignore_reason,
            payload=json.dumps(description.payload, ensure_ascii=False),
            source_created_at=(
                to_db_text(description.source_created_at) if description.source_created_at else None
            ),
            received_at=to_db_text(received_at),
        )
        .on_conflict_do_nothing()
        .returning(events.c.event_id)
    )
    if not inserted:
        return None
    if not description.handled:
        return NewEvent(job_id=None)
    job_id = await _enqueue_ledger_job(
        tx,
        source=source,
        account=account,
        event_id=event_id,
        payment_key=description.payment_key,
        retry_profile=retry_profile,
        now=received_at,
    )
    return NewEvent(job_id=job_id)


async def _enqueue_ledger_job(
    tx: Transaction,
    *,
    source: str,
    account: str,
    event_id: str,
    payment_key: str | None,
    retry_profile: RetryProfile,
    now: datetime,
) -> int:
    """Enqueue the payment's ledger job and point the event at it (`events.job_id`, F17)."""
    # A handled event always names its payment: the adapter stores one that doesn't as
    # ignored with reason `no_payment`.
    assert payment_key is not None
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
    return job_id


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
