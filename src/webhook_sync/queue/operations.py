"""The queue's operations on `jobs`, each one statement of queue.md run on a writer transaction.

Times come from the caller (the one clock, A14). `enqueue` runs inside the caller's own
transaction, so the change and the job it causes commit together (the outbox).
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from webhook_sync.clock import to_db_text
from webhook_sync.config import RetryProfile
from webhook_sync.db.schema import jobs
from webhook_sync.db.writer import Transaction
from webhook_sync.queue.retry import max_attempts

LIVE_STATUSES = ("waiting", "running")
"""A job in one of these is live: at most one per stage, sink and payment."""


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
