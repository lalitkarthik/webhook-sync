"""The queue: the six operations, backoff, error classes and pause (queue.md, ADR 0004).

`jobs.status` is written only here. The operations live in `operations.py`, the retry
profiles in `retry.py`, the stage runner and the reaper's loop in `runner.py`.
"""

from webhook_sync.queue.operations import (
    ClaimedJob,
    ErrorClass,
    Failure,
    LeaseLost,
    claim,
    complete,
    enqueue,
    fail,
    heartbeat,
    reap,
)
from webhook_sync.queue.pause import Pauses, PauseTarget
from webhook_sync.queue.retry import backoff, max_attempts
from webhook_sync.queue.runner import Done, StageFailed, StageRunner, Work, run_reaper

__all__ = [
    "ClaimedJob",
    "Done",
    "ErrorClass",
    "Failure",
    "LeaseLost",
    "PauseTarget",
    "Pauses",
    "StageFailed",
    "StageRunner",
    "Work",
    "backoff",
    "claim",
    "complete",
    "enqueue",
    "fail",
    "heartbeat",
    "max_attempts",
    "reap",
    "run_reaper",
]
