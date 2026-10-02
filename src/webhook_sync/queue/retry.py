"""The retry profiles: how many attempts a job is allowed (queue.md, "Backoff"; A8)."""

from webhook_sync.config import RetryProfile

_MAX_ATTEMPTS: dict[RetryProfile, int] = {"production": 10, "demo": 6}


def max_attempts(profile: RetryProfile) -> int:
    """The attempts a new job is allowed: 10 in the production profile, 6 in demo."""
    return _MAX_ATTEMPTS[profile]
