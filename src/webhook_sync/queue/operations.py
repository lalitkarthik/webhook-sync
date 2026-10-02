"""The queue's operations on `jobs`, each one statement of queue.md run on a writer transaction.

Times come from the caller (the one clock, A14). `enqueue` runs inside the caller's own
transaction, so the change and the job it causes commit together (the outbox).
"""

import logging
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from webhook_sync.clock import to_db_text
from webhook_sync.config import RetryProfile
from webhook_sync.db.schema import attempts, jobs
from webhook_sync.db.writer import Transaction, Writer
from webhook_sync.queue.pause import Pauses, PauseTarget
from webhook_sync.queue.retry import backoff, max_attempts

log = logging.getLogger("webhook_sync.queue")

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
    claimed_at: datetime
    """When this attempt started: the claim's `now`."""


class LeaseLost(Exception):
    """The job is no longer running under this claim token: the reaper or another worker has it.

    Raised by `complete` inside the caller's transaction, so the stale work's writes roll back.
    """

    def __init__(self, job_id: int) -> None:
        super().__init__(f"lease lost on job {job_id}")
        self.job_id = job_id


type ErrorClass = Literal["retry", "dead", "pause"]
"""The three error classes of queue.md: what a failed attempt does to its job."""


@dataclass(frozen=True)
class Failure:
    """Why a claimed job's attempt failed, as the stage's work reports it to `fail`.

    `retry_after` is the failing service's `Retry-After`, a minimum for the backoff. A `pause`
    failure names what it pauses: the job's source or its sink.
    """

    error: str
    error_class: ErrorClass
    retry_after: timedelta | None = None
    pause_target: PauseTarget | None = None

    def __post_init__(self) -> None:
        if (self.error_class == "pause") != (self.pause_target is not None):
            raise ValueError("a pause failure, and only a pause failure, names a pause target")


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
    if sink in pauses.sinks:
        return []
    token = uuid.uuid4().hex
    at = to_db_text(now)
    due = (
        sa.select(jobs.c.id)
        .where(
            jobs.c.stage == stage,
            jobs.c.sink == sink,
            jobs.c.source.not_in(pauses.sources),
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
            claimed_at=now,
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


async def complete(tx: Transaction, job: ClaimedJob, *, now: datetime) -> None:
    """Finish a claimed job inside the work's own transaction, fenced by its claim token.

    With `rerun` set the job goes straight back to waiting with its attempts reset; otherwise
    it is done. An `ok` attempt is recorded. If the token no longer holds the job, `LeaseLost`
    is raised, so the caller's transaction rolls back with the stale work's writes.
    """
    at = to_db_text(now)
    rerun = jobs.c.rerun == 1
    statement = (
        sa.update(jobs)
        .where(
            jobs.c.id == job.id,
            jobs.c.claim_token == job.claim_token,
            jobs.c.status == "running",
        )
        .values(
            status=sa.case((rerun, "waiting"), else_="done"),
            attempts=sa.case((rerun, 0), else_=jobs.c.attempts),
            run_after=at,
            rerun=0,
            claim_token=None,
            lease_until=None,
            updated_at=at,
        )
        .returning(jobs.c.id)
    )
    if not await tx.execute(statement):
        raise LeaseLost(job.id)
    await _record_attempt(tx, job, outcome="ok", error=None, now=now)


async def fail(
    writer: Writer,
    job: ClaimedJob,
    failure: Failure,
    *,
    pauses: Pauses,
    retry_profile: RetryProfile,
    now: datetime,
    rng: random.Random,
) -> Literal["waiting", "dead"]:
    """Record a failed attempt and move the job by its error class, in one fenced transaction.

    `retry`: back to waiting after the backoff (at least `retry_after`), or dead once the
    attempts are used up. `dead`: straight to dead. `pause`: the source or sink is paused and
    the job goes back to waiting with its attempt refunded. Returns the job's new status;
    raises `LeaseLost` (writing nothing, pausing nothing) if the token no longer holds the job.
    """
    at = to_db_text(now)
    status: Literal["waiting", "dead"] = "waiting"
    moves: dict[str, object] = {"rerun": 0, "run_after": at}
    if failure.error_class == "pause":
        moves["attempts"] = jobs.c.attempts - 1
    elif failure.error_class == "retry" and job.attempts < job.max_attempts:
        wait = backoff(retry_profile, job.attempts, rng=rng)
        if wait is None:
            status, moves = "dead", {}
        else:
            moves["run_after"] = to_db_text(now + max(wait, failure.retry_after or wait))
    else:
        status, moves = "dead", {}  # a dead job keeps its rerun flag and run_after: its history
    statement = (
        sa.update(jobs)
        .where(
            jobs.c.id == job.id,
            jobs.c.claim_token == job.claim_token,
            jobs.c.status == "running",
        )
        .values(
            status=status,
            claim_token=None,
            lease_until=None,
            last_error=failure.error,
            last_error_class=failure.error_class,
            updated_at=at,
            **moves,
        )
        .returning(jobs.c.id)
    )
    async with writer.transaction() as tx:
        if not await tx.execute(statement):
            raise LeaseLost(job.id)
        await _record_attempt(tx, job, outcome=failure.error_class, error=failure.error, now=now)
    if failure.pause_target == "source":
        pauses.pause("source", job.source)
    elif failure.pause_target == "sink":
        pauses.pause("sink", job.sink)
    return status


REAPED_ERROR = "lease expired"
"""The `last_error` and attempt error of a reaped job: its worker vanished or stalled."""


async def reap(
    writer: Writer, *, retry_profile: RetryProfile, now: datetime, rng: random.Random
) -> list[int]:
    """Take back every running job whose lease has expired, one row at a time.

    Each goes back to waiting after the backoff for its attempt, or to dead once its attempts
    are used up, with an `attempts` row `reaped`; its claim token is cleared, so the stalled
    worker's late completion matches nothing. A row that fails is logged and left for the next
    pass; the others are still reaped. Returns the ids taken back.
    """
    expired = sa.select(jobs.c.id).where(
        jobs.c.status == "running", jobs.c.lease_until < to_db_text(now)
    )
    return await _take_back(writer, expired, retry_profile=retry_profile, now=now, rng=rng)


async def reclaim(
    writer: Writer, *, retry_profile: RetryProfile, now: datetime, rng: random.Random
) -> list[int]:
    """At start, before serving: take back every job still running, lease expired or not.

    One server per database (ADR 0007) makes each a leftover of a crashed run (A13). Each is
    reaped as `reap` does it: its attempt counted, backoff or dead, an `attempts` row `reaped`,
    so a job that crashes the process every time still reaches dead. Returns the ids.
    """
    running = sa.select(jobs.c.id).where(jobs.c.status == "running")
    return await _take_back(writer, running, retry_profile=retry_profile, now=now, rng=rng)


async def _take_back(
    writer: Writer,
    which: sa.Select[tuple[int]],
    *,
    retry_profile: RetryProfile,
    now: datetime,
    rng: random.Random,
) -> list[int]:
    """Reap the running jobs `which` selects, each in its own transaction."""
    async with writer.transaction() as tx:
        ids = [row["id"] for row in await tx.execute(which.order_by(jobs.c.id))]
    taken: list[int] = []
    for job_id in ids:
        try:
            async with writer.transaction() as tx:
                if await _reap_one(tx, which.where(jobs.c.id == job_id), retry_profile, now, rng):
                    taken.append(job_id)
        except Exception:
            log.exception("job not reaped", extra={"job_id": job_id})
    return taken


async def _reap_one(
    tx: Transaction,
    which: sa.Select[tuple[int]],
    retry_profile: RetryProfile,
    now: datetime,
    rng: random.Random,
) -> bool:
    """Reap one job if `which` still selects it (a heartbeat may have saved it meanwhile)."""
    rows = await tx.execute(sa.select(jobs).where(jobs.c.id.in_(which.scalar_subquery())))
    if not rows:
        return False
    [job] = rows
    at = to_db_text(now)
    wait = backoff(retry_profile, job["attempts"], rng=rng)
    moves: dict[str, object] = {"status": "dead"}
    if job["attempts"] < job["max_attempts"] and wait is not None:
        moves = {"status": "waiting", "rerun": 0, "run_after": to_db_text(now + wait)}
    await tx.execute(
        sa.update(jobs)
        .where(jobs.c.id == job["id"], jobs.c.claim_token == job["claim_token"])
        .values(
            claim_token=None,
            lease_until=None,
            last_error=REAPED_ERROR,
            last_error_class="retry",
            updated_at=at,
            **moves,
        )
    )
    await tx.execute(
        sa.insert(attempts).values(
            job_id=job["id"],
            number=job["attempts"],
            started_at=None,
            finished_at=at,
            outcome="reaped",
            error=REAPED_ERROR,
            duration_ms=None,
        )
    )
    return True


async def _record_attempt(
    tx: Transaction, job: ClaimedJob, *, outcome: str, error: str | None, now: datetime
) -> None:
    """Append the claimed attempt's row to `attempts`: the job's timeline."""
    await tx.execute(
        sa.insert(attempts).values(
            job_id=job.id,
            number=job.attempts,
            started_at=to_db_text(job.claimed_at),
            finished_at=to_db_text(now),
            outcome=outcome,
            error=error,
            duration_ms=round((now - job.claimed_at).total_seconds() * 1000),
        )
    )
