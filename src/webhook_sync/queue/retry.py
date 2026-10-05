"""The retry profiles: the backoff tables and the attempts a job is allowed (queue.md; A8).

The idea of a fixed table of waits is from svix-webhooks
(https://github.com/svix/svix-webhooks, commit cb056ba): MIT, Copyright (c) 2021 Svix.
Changed: see THIRD_PARTY_NOTICES.md.
"""

import random
from datetime import timedelta

from webhook_sync.config import RetryProfile

_S = timedelta(seconds=1)
_M = timedelta(minutes=1)
_H = timedelta(hours=1)

_WAITS: dict[RetryProfile, tuple[timedelta, ...]] = {
    "production": (5 * _S, 30 * _S, 2 * _M, 10 * _M, 30 * _M, 1 * _H, 2 * _H, 4 * _H, 8 * _H),
    "demo": (1 * _S, 2 * _S, 4 * _S, 8 * _S, 16 * _S),
}
"""The wait after each failed attempt, before jitter; the attempt after the last is dead."""

JITTER = (0.5, 1.5)
"""Each wait is multiplied by a random factor in this range, so it is never zero."""


def max_attempts(profile: RetryProfile) -> int:
    """The attempts a new job is allowed: 10 in the production profile, 6 in demo."""
    return len(_WAITS[profile]) + 1


def backoff(profile: RetryProfile, failed_attempt: int, *, rng: random.Random) -> timedelta | None:
    """How long to wait after attempt `failed_attempt` (from 1) failed, or None: dead.

    The table's wait times a random factor between 0.5 and 1.5, drawn from `rng`.
    """
    waits = _WAITS[profile]
    if failed_attempt > len(waits):
        return None
    return waits[failed_attempt - 1] * rng.uniform(*JITTER)
