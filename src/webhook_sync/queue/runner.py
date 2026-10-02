"""The stage runner: claim a stage's due jobs, run its work, complete or fail each (queue.md).

A stage plugs in only its work: a function from a claimed job to `Done` (with the writes to
commit alongside `complete`), or a raised `StageFailed` naming the error class. Time comes from
the one clock (A14); the sleeps are injected, so tests drive the runner without sleeping.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from webhook_sync.clock import utc_now
from webhook_sync.config import RetryProfile
from webhook_sync.db.writer import Transaction, Writer
from webhook_sync.queue.operations import ClaimedJob, Failure, claim, complete, fail
from webhook_sync.queue.pause import Pauses

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


type Work = Callable[[ClaimedJob], Awaitable[Done]]
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
        for job in claimed:
            await self._run(job)
        return len(claimed)

    async def _run(self, job: ClaimedJob) -> None:
        try:
            done = await self._work(job)
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
