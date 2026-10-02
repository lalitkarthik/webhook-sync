"""The per-source request-rate cap (receiver.md check 2): a token bucket, 50 a second, burst 50 (A18).

In memory, one bucket per source. Time comes from the one clock, so tests move it with
time-machine.
"""

from dataclasses import dataclass
from datetime import datetime

from webhook_sync.clock import utc_now

RATE_PER_SECOND = 50.0
BURST = 50.0


@dataclass
class _Bucket:
    tokens: float
    updated: datetime


class RateCap:
    def __init__(self, *, rate: float = RATE_PER_SECOND, burst: float = BURST) -> None:
        self._rate = rate
        self._burst = burst
        self._buckets: dict[str, _Bucket] = {}

    def allow(self, source: str) -> bool:
        """Take one token from the source's bucket; False when it is empty (answer 429)."""
        now = utc_now()
        bucket = self._buckets.setdefault(source, _Bucket(self._burst, now))
        # Subtracting datetimes keeps whole microseconds; a clock stepped back refills nothing.
        elapsed = max(0.0, (now - bucket.updated).total_seconds())
        bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate)
        bucket.updated = now
        if bucket.tokens < 1:
            return False
        bucket.tokens -= 1
        return True
