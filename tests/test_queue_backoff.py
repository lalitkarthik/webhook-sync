"""The backoff tables of queue.md, tested directly: they are pure."""

import random
from datetime import timedelta

import pytest

from webhook_sync.config import RetryProfile
from webhook_sync.queue import backoff, max_attempts

S = timedelta(seconds=1)
M = timedelta(minutes=1)
H = timedelta(hours=1)

TABLES: dict[RetryProfile, list[timedelta]] = {
    "production": [5 * S, 30 * S, 2 * M, 10 * M, 30 * M, 1 * H, 2 * H, 4 * H, 8 * H],
    "demo": [1 * S, 2 * S, 4 * S, 8 * S, 16 * S],
}


class Extreme(random.Random):
    """A random source whose every draw is the bottom or the top of the range."""

    def __init__(self, top: bool) -> None:
        super().__init__()
        self.top = top

    def uniform(self, a: float, b: float) -> float:
        return b if self.top else a


@pytest.mark.parametrize("profile", ["production", "demo"])
def test_each_attempts_wait_lies_in_its_half_to_one_and_a_half_band(
    profile: RetryProfile,
) -> None:
    rng = random.Random(12)
    for attempt, wait in enumerate(TABLES[profile], start=1):
        assert backoff(profile, attempt, rng=Extreme(top=False)) == wait / 2
        assert backoff(profile, attempt, rng=Extreme(top=True)) == wait * 3 / 2
        for _ in range(50):
            drawn = backoff(profile, attempt, rng=rng)
            assert drawn is not None
            assert wait / 2 <= drawn <= wait * 3 / 2


@pytest.mark.parametrize(("profile", "dead_after"), [("production", 10), ("demo", 6)])
def test_the_job_is_dead_once_its_last_attempt_fails(
    profile: RetryProfile, dead_after: int
) -> None:
    assert max_attempts(profile) == dead_after
    assert backoff(profile, dead_after - 1, rng=random.Random()) is not None
    assert backoff(profile, dead_after, rng=random.Random()) is None
