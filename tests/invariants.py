"""Settle and the nine invariants (testing.md): what every scenario, crash test and load check
ends with.

`settle` waits until no job is waiting or running, and fails at once on a source or sink paused
that the test did not pause on purpose (F22). Import with `from invariants import ...`.
"""

import asyncio
import time
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from webhook_sync.db.reader import Reader

SETTLE_LIMIT = 30.0
"""Real seconds a run gets to settle, even on a busy machine."""

OPEN_JOBS = (
    "SELECT id, stage, sink, source, payment_key, status FROM jobs"
    " WHERE status IN ('waiting', 'running') ORDER BY id"
)
PAUSED = "SELECT name, kind, reason FROM sinks WHERE state = 'paused' ORDER BY name"


async def settle(
    data_dir: Path, *, paused: Collection[str] = (), limit: float = SETTLE_LIMIT
) -> None:
    """Return once no job is waiting or running, but for a waiting job a source or sink paused
    on purpose (`paused`, by name) holds. Fail at once on any other pause, and after `limit`
    real seconds naming the jobs still open."""
    deadline = time.monotonic() + limit
    async with Reader.open(data_dir) as reader:
        while True:
            for pause in await reader.fetch_all(PAUSED):
                if pause["name"] not in paused:
                    raise AssertionError(
                        f"{pause['kind']} {pause['name']} paused, not on purpose: {pause['reason']}"
                    )
            still = [job for job in await reader.fetch_all(OPEN_JOBS) if not _held(job, paused)]
            if not still:
                return
            if time.monotonic() > deadline:
                named = ", ".join(_job(job) for job in still)
                raise AssertionError(f"not settled after {limit} s; jobs still open: {named}")
            await asyncio.sleep(0.01)


def _held(job: Mapping[str, Any], paused: Collection[str]) -> bool:
    """A waiting job a pause on purpose holds: its source's, or its sink's."""
    return job["status"] == "waiting" and (job["source"] in paused or job["sink"] in paused)


def _job(job: Mapping[str, Any]) -> str:
    """`job 3 (mirror sheets, pay_X) waiting`; a ledger job's sink is the ledger itself."""
    stage = job["stage"] if job["sink"] == job["stage"] else f"{job['stage']} {job['sink']}"
    return f"job {job['id']} ({stage}, {job['payment_key']}) {job['status']}"
