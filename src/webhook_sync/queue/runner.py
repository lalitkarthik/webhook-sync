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
from webhook_sync.queue.operations import ClaimedJob, claim, complete
from webhook_sync.queue.pause import Pauses

type Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class Done:
    """The work succeeded. `writes`, if given, runs in the same transaction as `complete`, so a
    stale worker's writes roll back with its fenced completion."""

    writes: Callable[[Transaction], Awaitable[None]] | None = None


type Work = Callable[[ClaimedJob], Awaitable[Done]]
"""A stage's work on one claimed job."""


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
        self._retry_profile = retry_profile
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
            done = await self._work(job)
            async with self._writer.transaction() as tx:
                if done.writes is not None:
                    await done.writes(tx)
                await complete(tx, job, now=utc_now())
        return len(claimed)
