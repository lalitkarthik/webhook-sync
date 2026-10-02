"""The stage runner: claim a stage's due jobs, run its work, complete or fail each (queue.md).

A stage plugs in only its work: a function from a claimed job to `Done` (with the writes to
commit alongside `complete`), or a raised `StageFailed` naming the error class. Time comes from
the one clock (A14); the sleeps are injected, so tests drive the runner without sleeping.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from webhook_sync.clock import utc_now
from webhook_sync.config import RetryProfile
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
)
from webhook_sync.queue.pause import Pauses

log = logging.getLogger("webhook_sync.queue")

type Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Done:
    """The work succeeded. `writes`, if given, runs in the same transaction as `complete`, so a
    stale worker's writes roll back with its fenced completion."""

    writes: Callable[[Transaction], Awaitable[None]] | None = None


class StageFailed(Exception):
    """Raised by a stage's work: the attempt failed, and `failure` says how (its error class)."""

    def __init__(self, failure: Failure) -> None:
        super().__init__(failure.error)
        self.failure = failure


type Work = Callable[[ClaimedJob], Coroutine[Any, Any, Done]]
"""A stage's work on one claimed job: returns `Done` or raises `StageFailed`. Any other
exception is our own bug, so the job goes to dead (queue.md's error classes)."""


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
        """Claim and run due jobs until `stop`; wait a second whenever nothing is due."""
        while not self._stopping:
            if await self.run_once() == 0 and not self._stopping:
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
        except StageFailed as failed:
            await self._fail(job, failed.failure)
            return
        except Exception as error:  # our own bug: retrying can't help
            await self._fail(job, Failure(f"{type(error).__name__}: {error}", "dead"))
            return
        async with self._writer.transaction() as tx:
            if done.writes is not None:
                await done.writes(tx)
            await complete(tx, job, now=utc_now())

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
