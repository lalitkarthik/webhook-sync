"""The queue's operations on `jobs`, each one statement of queue.md run on a writer transaction.

Times come from the caller (the one clock, A14). `enqueue` runs inside the caller's own
transaction, so the change and the job it causes commit together (the outbox).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from webhook_sync.clock import to_db_text
from webhook_sync.config import RetryProfile
from webhook_sync.db.schema import jobs
from webhook_sync.db.writer import Transaction, Writer
from webhook_sync.queue.pause import Pauses
from webhook_sync.queue.retry import max_attempts

LIVE_STATUSES = ("waiting", "running")
"""A job in one of these is live: at most one per stage, sink and payment."""

LEASE = timedelta(seconds=60)
"""How long a claim or a heartbeat holds a job before the reaper may take it back."""


@dataclass(frozen=True)
class ClaimedJob:
    """A job `claim` handed out: what the stage's work needs, and the token that fences it."""

    id: int
    claim_token: str
    stage: str
    sink: str
    source: str
    account: str
    payment_key: str
    attempts: int
    max_attempts: int
    rerun: bool
    prefetched: str | None
    caused_by_event_id: str | None


async def enqueue(
    tx: Transaction,
    *,
    stage: str,
    sink: str,
    source: str,
    account: str,
    payment_key: str,
    event_id: str | None,
    retry_profile: RetryProfile,
    now: datetime,
) -> int:
    """Insert a waiting job for the payment, or fold the work into its one live job.

    A running live job gets `rerun` set; a live job has `prefetched` cleared (a newer event
    beats a listed state) and `caused_by_event_id` updated; `run_after` is left alone, so a job
    in backoff stays in backoff. Returns the job's id. (P4's backfill adds a `prefetched` state.)
    """
    at = to_db_text(now)
    insert = sqlite.insert(jobs).values(
        stage=stage,
        sink=sink,
        source=source,
        account=account,
        payment_key=payment_key,
        status="waiting",
        rerun=0,
        attempts=0,
        max_attempts=max_attempts(retry_profile),
        run_after=at,
        caused_by_event_id=event_id,
        created_at=at,
        updated_at=at,
    )
    statement = insert.on_conflict_do_update(
        index_elements=[
            jobs.c.stage,
            jobs.c.sink,
            jobs.c.source,
            jobs.c.account,
            jobs.c.payment_key,
        ],
        index_where=jobs.c.status.in_(LIVE_STATUSES),
        set_={
            "rerun": sa.case((jobs.c.status == "running", 1), else_=jobs.c.rerun),
            "prefetched": None,
            "caused_by_event_id": insert.excluded.caused_by_event_id,
            "updated_at": insert.excluded.updated_at,
        },
    ).returning(jobs.c.id)
    [row] = await tx.execute(statement)
    return row["id"]


async def claim(
    writer: Writer,
    *,
    stage: str,
    sink: str,
    limit: int,
    claimed_by: str,
    pauses: Pauses,
    now: datetime,
) -> list[ClaimedJob]:
    """Take up to `limit` due waiting jobs of a stage and sink, oldest `run_after` first.

    Each gets a 60 s lease, one shared new claim token, `claimed_by` (the server's instance id
    and start time) and one more attempt. Nothing of a paused source or sink is taken.
    """
    token = uuid.uuid4().hex
    at = to_db_text(now)
    due = (
        sa.select(jobs.c.id)
        .where(
            jobs.c.stage == stage,
            jobs.c.sink == sink,
            jobs.c.status == "waiting",
            jobs.c.run_after <= at,
        )
        .order_by(jobs.c.run_after, jobs.c.id)
        .limit(limit)
    )
    statement = (
        sa.update(jobs)
        .where(jobs.c.id.in_(due.scalar_subquery()))
        .values(
            status="running",
            claim_token=token,
            claimed_by=claimed_by,
            lease_until=to_db_text(now + LEASE),
            attempts=jobs.c.attempts + 1,
            updated_at=at,
        )
        .returning(*jobs.c)
    )
    async with writer.transaction() as tx:
        rows = await tx.execute(statement)
    rows.sort(key=lambda row: (row["run_after"], row["id"]))
    return [
        ClaimedJob(
            id=row["id"],
            claim_token=token,
            stage=row["stage"],
            sink=row["sink"],
            source=row["source"],
            account=row["account"],
            payment_key=row["payment_key"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            rerun=bool(row["rerun"]),
            prefetched=row["prefetched"],
            caused_by_event_id=row["caused_by_event_id"],
        )
        for row in rows
    ]


async def heartbeat(writer: Writer, ids: list[int], token: str, *, now: datetime) -> set[int]:
    """Push the lease of each job still held under `token` to 60 s past now.

    Returns the ids whose lease was lost (no longer running under this token): the worker
    stops those without writing. An empty set means every lease is still held.
    """
    statement = (
        sa.update(jobs)
        .where(jobs.c.id.in_(ids), jobs.c.claim_token == token, jobs.c.status == "running")
        .values(lease_until=to_db_text(now + LEASE), updated_at=to_db_text(now))
        .returning(jobs.c.id)
    )
    async with writer.transaction() as tx:
        rows = await tx.execute(statement)
    return set(ids) - {row["id"] for row in rows}
