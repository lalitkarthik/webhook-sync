"""The load checks' harness: deliveries sent all at once against the receiver's rate cap, and
what a run's numbers are made of (Q171, Q172).

`send_at_once` sends every delivery at the same moment. The default 50-a-second cap answers
what it can't take 429 `rate_capped`, and each refused delivery is sent again, under the cap,
until it is not refused, as Razorpay's retries would. `flushes` reads the flusher's
"sheet flush" lines from a server's log, and gives each the Sheets calls made during it.
"""

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fakes.razorpay import Delivery, FakeRazorpay, Sent
from fakes.sheets import FakeSheets

from webhook_sync.receiver.rate_cap import RATE_PER_SECOND

RETRIED_PER_SECOND = int(RATE_PER_SECOND * 0.8)
"""How many refused deliveries are sent again each second, all told: under the cap."""
RATE_CAPPED = (429, {"rejection": "rate_capped"})


@dataclass(frozen=True)
class Sends:
    """Every delivery's answer when all were sent at once, and the answers to the refused
    ones sent again under the cap."""

    at_once: list[Sent]
    again: list[Sent]

    @property
    def taken(self) -> list[Sent]:
        """Every answer that wasn't the cap's refusal."""
        return [one for one in [*self.at_once, *self.again] if one.status_code != 429]


async def send_at_once(
    razorpay: FakeRazorpay, client: httpx.AsyncClient, deliveries: list[Delivery]
) -> Sends:
    """Send every delivery at once; each the cap refuses goes again, one send after another,
    `RETRIED_PER_SECOND` a second all told, until it is not refused."""
    cap = UnderTheCap(RETRIED_PER_SECOND)

    async def send(delivery: Delivery) -> tuple[list[Sent], list[Sent]]:
        first = await razorpay.deliver(client, delivery)
        again: list[Sent] = []
        last = first
        while last and last[-1].status_code == 429:
            await cap.wait()
            last = await razorpay.deliver(client, delivery)
            again += last
        return first, again

    sends = await asyncio.gather(*(send(delivery) for delivery in deliveries))
    return Sends(
        at_once=[one for first, _ in sends for one in first],
        again=[one for _, again in sends for one in again],
    )


class UnderTheCap:
    """Lets `per_second` sends through a second, evenly spaced."""

    def __init__(self, per_second: int) -> None:
        self._gap = 1 / per_second
        self._next = time.monotonic()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(self._next, now) + self._gap
        await asyncio.sleep(delay)


def percentile(values: list[float], p: int) -> float:
    """The nearest-rank `p`th percentile."""
    ordered = sorted(values)
    return ordered[math.ceil(p / 100 * len(ordered)) - 1]


@dataclass
class Flush:
    """One flush the server logged: when it ran, how many jobs it claimed, and the Sheets
    calls FakeSheets answered while it ran."""

    started: datetime
    ended: datetime
    jobs: int
    calls: list[str] = field(default_factory=list[str])


def flushes(log: Path, sheets: FakeSheets) -> list[Flush]:
    """Every flush in a server's log, in order, each with the calls answered during it. A call
    answered outside every flush fails: the flusher is the only caller."""
    found: list[Flush] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        if not line.startswith("{"):
            continue
        entry = json.loads(line)
        if entry.get("message") != "sheet flush":
            continue
        ended = datetime.fromisoformat(entry["time"])
        started = ended - timedelta(milliseconds=entry["duration_ms"] + 1)
        found.append(Flush(started, ended, entry["jobs"]))
    for name, at in zip(sheets.calls, sheets.called_at, strict=True):
        during = [flush for flush in found if flush.started <= at <= flush.ended]
        assert during, f"a {name} call at {at} came outside every logged flush"
        during[0].calls.append(name)
    return found
