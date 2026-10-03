"""The stage runner: claim a stage's due jobs, run its work, complete or fail each (queue.md).

A stage plugs in only its work: a function from a claimed job to `Done` (with the writes to
commit alongside `complete`), or a raised `StageFailed` naming the error class. Time comes from
the one clock (A14); the sleeps are injected, so tests drive the runner without sleeping.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import sqlalchemy as sa

from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.config import RetryProfile
from webhook_sync.db.schema import jobs
from webhook_sync.db.writer import Transaction, Writer
from webhook_sync.queue.operations import (
    ClaimedJob,
    Failure,
    LeaseLost,
    claim,
    complete,
    fail,
    heartbeat,
    reap,
    release,
)
from webhook_sync.queue.pause import Pauses

log = logging.getLogger("webhook_sync.queue")

type Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Done:
    """The work succeeded. `writes`, if given, runs in the same transaction as `complete`, so a
    stale worker's writes roll back with its fenced completion.

    `failed` (batch stages only) names jobs of the batch that failed on their own, by job id:
    those fail with their class after the rest complete."""

    writes: Callable[[Transaction], Awaitable[None]] | None = None
    failed: Mapping[int, Failure] = field(default_factory=dict[int, Failure])


class StageFailed(Exception):
    """Raised by a stage's work: the attempt failed, and `failure` says how (its error class)."""

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.error)
        self.failure = failure


type Work = Callable[[ClaimedJob], Coroutine[Any, Any, Done]]
"""A stage's work on one claimed job: returns `Done` or raises `StageFailed`. Any other
exception is our own bug, so the job goes to dead (queue.md's error classes)."""


type BatchWork = Callable[[list[ClaimedJob]], Coroutine[Any, Any, Done]]
"""A batch stage's work on every claimed job at once (A12): returns `Done`, whose writes commit
with every job's `complete` but those in its `failed`, or raises `StageFailed`, which fails
every job with its class."""


class StageRunner:
    """Runs one stage's jobs for one sink: claim, work with a heartbeat, complete or fail."""

    def __init__(
        self,
        writer: Writer,
        *,
        stage: str,
        sink: str,
        limit: int,
        work: Work,
        claimed_by: str,
        pauses: Pauses,
        retry_profile: RetryProfile,
        rng: random.Random | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._writer = writer
        self._stage = stage
        self._sink = sink
        self._limit = limit
        self._work = work
        self._claimed_by = claimed_by
        self._pauses = pauses
        self._retry_profile: RetryProfile = retry_profile
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._stopping = False

    async def run(self) -> None:
        """Claim and run due jobs until `stop`; wait a second whenever nothing is due. A pass
        that raises is logged and the next one tried; a job it left running is reaped."""
        while not self._stopping:
            try:
                claimed = await self.run_once()
            except Exception:
                log.exception("stage runner pass failed; trying again shortly")
                claimed = 0
            if claimed == 0 and not self._stopping:
                await self._sleep(IDLE_WAIT.total_seconds())

    def stop(self) -> None:
        """Stop claiming: `run` returns once the batch it holds is finished (shutdown, #14)."""
        self._stopping = True

    async def run_once(self) -> int:
        """Claim the due jobs (up to the limit) and run each; returns how many were claimed."""
        claimed = await claim(
            self._writer,
            stage=self._stage,
            sink=self._sink,
            limit=self._limit,
            claimed_by=self._claimed_by,
            pauses=self._pauses,
            now=utc_now(),
        )
        if not claimed:
            return 0
        leases = _Leases(self._writer, claimed, self._sleep)
        beating = asyncio.create_task(leases.keep_alive())
        try:
            for job in claimed:
                await self._run(job, leases)
        finally:
            beating.cancel()
        return len(claimed)

    async def _run(self, job: ClaimedJob, leases: _Leases) -> None:
        try:
            await self._attempt(job, leases)
        except LeaseLost:
            # The reaper or another worker has the job: this attempt's result is discarded.
            log.warning("lease lost: result discarded", extra={"job_id": job.id})
        finally:
            leases.release(job.id)

    async def _attempt(self, job: ClaimedJob, leases: _Leases) -> None:
        try:
            done = await leases.guard(job.id, self._work(job))
            async with self._writer.transaction() as tx:
                if done.writes is not None:
                    await done.writes(tx)
                await complete(tx, job, now=utc_now())
        except LeaseLost:
            raise
        except StageFailed as failed:
            await self._fail(job, failed.failure)
        except Exception as error:  # our own bug: retrying can't help
            await self._fail(job, Failure(f"{type(error).__name__}: {error}", "dead"))

    async def _fail(self, job: ClaimedJob, failure: Failure) -> None:
        await fail(
            self._writer,
            job,
            failure,
            pauses=self._pauses,
            retry_profile=self._retry_profile,
            now=utc_now(),
            rng=self._rng,
        )


class BatchStageRunner:
    """Runs one batch stage for one sink (A12): claim up to `limit` due jobs, one work call for
    all of them, then complete them all in one transaction or fail them all.

    It claims again at once after a full batch; otherwise it waits `every` before the next
    claim, checking each second and claiming at once when `limit` jobs are due."""

    def __init__(
        self,
        writer: Writer,
        *,
        stage: str,
        sink: str,
        limit: int,
        every: timedelta,
        work: BatchWork,
        claimed_by: str,
        pauses: Pauses,
        retry_profile: RetryProfile,
        rng: random.Random | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._writer = writer
        self._stage = stage
        self._sink = sink
        self._limit = limit
        self._every = every
        self._work = work
        self._claimed_by = claimed_by
        self._pauses = pauses
        self._retry_profile: RetryProfile = retry_profile
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._stopping = False

    async def run(self) -> None:
        """Run batches until `stop`. A pass that raises is logged and the next one tried; jobs
        it left running are reaped."""
        while not self._stopping:
            try:
                claimed = await self.run_once()
            except Exception:
                log.exception("batch stage pass failed; trying again shortly")
                claimed = 0
            if claimed < self._limit:
                await self._wait()

    def stop(self) -> None:
        """Stop claiming: `run` returns once the batch it holds is finished."""
        self._stopping = True

    async def _wait(self) -> None:
        """Wait `every`, a second at a time, ending early when a full batch is due."""
        waited = timedelta(0)
        while waited < self._every and not self._stopping:
            await self._sleep(IDLE_WAIT.total_seconds())
            waited += IDLE_WAIT
            try:
                if await self._due() >= self._limit:
                    return
            except Exception:
                log.exception("counting due jobs failed; waiting on")

    async def _due(self) -> int:
        """How many jobs `claim` would hand out now, uncapped."""
        if self._sink in self._pauses.sinks:
            return 0
        statement = sa.select(sa.func.count()).where(
            jobs.c.stage == self._stage,
            jobs.c.sink == self._sink,
            jobs.c.source.not_in(self._pauses.sources),
            jobs.c.status == "waiting",
            jobs.c.run_after <= to_db_text(utc_now()),
        )
        async with self._writer.transaction() as tx:
            [row] = await tx.execute(statement)
        return int(row[0])

    async def run_once(self) -> int:
        """Claim up to `limit` due jobs and run the batch; returns how many were claimed."""
        claimed = await claim(
            self._writer,
            stage=self._stage,
            sink=self._sink,
            limit=self._limit,
            claimed_by=self._claimed_by,
            pauses=self._pauses,
            now=utc_now(),
        )
        if not claimed:
            return 0
        leases = _Leases(self._writer, claimed, self._sleep)
        beating = asyncio.create_task(leases.keep_alive())
        try:
            await self._attempt(claimed)
        finally:
            beating.cancel()
        return len(claimed)

    async def _attempt(self, claimed: list[ClaimedJob]) -> None:
        try:
            done = await self._work(claimed)
        except StageFailed as failed:
            await self._fail_all(claimed, failed.failure)
            return
        except Exception as error:  # our own bug: retrying can't help
            await self._fail_all(claimed, Failure(f"{type(error).__name__}: {error}", "dead"))
            return
        async with self._writer.transaction() as tx:
            if done.writes is not None:
                await done.writes(tx)
            for job in claimed:
                if job.id in done.failed:
                    continue
                try:
                    await complete(tx, job, now=utc_now())
                except LeaseLost:
                    # The reaper has it back: it runs again, and writing by key is idempotent.
                    log.warning("lease lost: result discarded", extra={"job_id": job.id})
        for job in claimed:
            if job.id in done.failed:
                await self._fail_all([job], done.failed[job.id])

    async def _fail_all(self, claimed: list[ClaimedJob], failure: Failure) -> None:
        for job in claimed:
            try:
                await fail(
                    self._writer,
                    job,
                    failure,
                    pauses=self._pauses,
                    retry_profile=self._retry_profile,
                    now=utc_now(),
                    rng=self._rng,
                )
            except LeaseLost:
                log.warning("lease lost: result discarded", extra={"job_id": job.id})


SHUTDOWN_GRACE = timedelta(seconds=20)
"""How long running jobs get to finish at shutdown before their leases are released."""


class Runners:
    """The server's stage runners: started together, and stopped together at shutdown."""

    def __init__(
        self,
        writer: Writer,
        runners: Sequence[StageRunner | BatchStageRunner],
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._writer = writer
        self._runners = list(runners)
        self._sleep = sleep
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        self._tasks = [asyncio.create_task(runner.run()) for runner in self._runners]

    async def stop(self) -> list[int]:
        """Stop claiming, give running jobs up to 20 s, cancel what is still working, then
        release every job left running on a fresh transaction (queue.md, A13). Returns the ids
        released."""
        for runner in self._runners:
            runner.stop()
        if self._tasks:
            finished = asyncio.gather(*self._tasks, return_exceptions=True)
            grace = asyncio.ensure_future(self._sleep(SHUTDOWN_GRACE.total_seconds()))
            await asyncio.wait([finished, grace], return_when=asyncio.FIRST_COMPLETED)
            grace.cancel()
            for task in self._tasks:
                task.cancel()
            await asyncio.wait([finished, grace])
        released = await release(self._writer, now=utc_now())
        if released:
            log.info("unfinished jobs released at shutdown: %d", len(released))
        return released


REAP_EVERY = timedelta(seconds=30)
"""How often the reaper takes back expired leases."""


async def run_reaper(
    writer: Writer,
    *,
    retry_profile: RetryProfile,
    rng: random.Random | None = None,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """The reaper: every 30 s, return jobs whose lease has expired (queue.md). Runs until
    cancelled; a failed pass is logged and the next one tried. The first pass is 30 s in:
    start has just reclaimed every running job, so none can have expired before then."""
    rng = rng or random.Random()
    while True:
        await sleep(REAP_EVERY.total_seconds())
        try:
            await reap(writer, retry_profile=retry_profile, now=utc_now(), rng=rng)
        except Exception:
            log.exception("reaper pass failed; trying again at the next pass")


IDLE_WAIT = timedelta(seconds=1)
"""How long a runner waits before claiming again when nothing was due."""

HEARTBEAT_EVERY = timedelta(seconds=20)
"""How often a running batch pushes its leases forward (a third of the 60 s lease)."""


class _Leases:
    """One claimed batch's leases: a heartbeat every 20 s for the jobs not yet finished; the
    work of a job whose lease is lost is cancelled, so it stops without writing."""

    def __init__(self, writer: Writer, claimed: list[ClaimedJob], sleep: Sleep) -> None:
        self._writer = writer
        self._token = claimed[0].claim_token
        self._sleep = sleep
        self._held = {job.id for job in claimed}
        self._lost: set[int] = set()
        self._working: tuple[int, asyncio.Task[Done]] | None = None

    async def keep_alive(self) -> None:
        while self._held:
            await self._sleep(HEARTBEAT_EVERY.total_seconds())
            try:
                lost = await heartbeat(self._writer, sorted(self._held), self._token, now=utc_now())
            except Exception:
                log.exception("heartbeat failed; trying again at the next beat")
                continue
            self._held -= lost
            self._lost |= lost
            if self._working is not None and self._working[0] in lost:
                self._working[1].cancel()

    async def guard(self, job_id: int, work: Coroutine[Any, Any, Done]) -> Done:
        """Run a job's work, raising `LeaseLost` if a heartbeat finds its lease gone."""
        task = asyncio.create_task(work)
        if job_id in self._lost:
            task.cancel()
        self._working = (job_id, task)
        try:
            return await task
        except asyncio.CancelledError:
            if job_id in self._lost and not _cancelling():
                raise LeaseLost(job_id) from None
            raise
        finally:
            self._working = None

    def release(self, job_id: int) -> None:
        """The job is finished: stop heartbeating it."""
        self._held.discard(job_id)


def _cancelling() -> bool:
    """Whether the current task itself is being cancelled, not only the work inside it."""
    current = asyncio.current_task()
    return current is not None and current.cancelling() > 0
