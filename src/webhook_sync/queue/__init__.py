"""The queue: the six operations, backoff, error classes and pause (queue.md, ADR 0004).

`jobs.status` is written only here. The operations live in `operations.py`, the retry
profiles in `retry.py`.
"""

from webhook_sync.queue.operations import enqueue
from webhook_sync.queue.retry import max_attempts

__all__ = ["enqueue", "max_attempts"]
