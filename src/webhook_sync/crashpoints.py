"""Crash points (testing.md, Q159): `WSYNC_CRASH_AT=<name>` kills the server at a named place.

`wsync serve` reads the variable once at start (`arm`). It refuses it unless the Razorpay key is
a test-mode key, so it can't take a buyer's server down, and refuses a name it doesn't know, so
a typo can't make a crash test pass by crashing nowhere; armed, it logs a warning. At the named
place `crash_point` calls `os._exit` with `CRASH_EXIT_CODE`: no `finally`, no shutdown, no
flush, as in a real crash (F21). Unarmed, `crash_point` does nothing.
"""

import logging
import os
from collections.abc import Mapping

from webhook_sync.config import Config, ConfigError

log = logging.getLogger("webhook_sync.crashpoints")

VARIABLE = "WSYNC_CRASH_AT"
TEST_KEY_PREFIX = "rzp_test_"
CRASH_EXIT_CODE = 86
"""The exit code of every crash point: a crash test fails unless the server exited with it (A15)."""

NAMES = ("after_event_commit", "after_sheet_write_before_done")
"""`after_event_commit`: the receiver, after the event's transaction commits and before it
answers; `after_sheet_write_before_done`: the flusher, after its post-write check and before
its jobs are completed (A14)."""

_armed: str | None = None


def arm(config: Config, environ: Mapping[str, str]) -> None:
    """Arm the crash point `environ` names, if any; raise ConfigError when it is refused."""
    global _armed
    name = environ.get(VARIABLE)
    if not name:
        return
    if not config.razorpay_secrets.key_id.startswith(TEST_KEY_PREFIX):
        raise ConfigError(
            f"{VARIABLE} needs a test-mode key: RAZORPAY_KEY_ID must start {TEST_KEY_PREFIX}"
        )
    if name not in NAMES:
        raise ConfigError(
            f"{VARIABLE} names no crash point the server knows: {name!r};"
            f" it knows {', '.join(NAMES)}"
        )
    _armed = name
    log.warning("crash point armed: the server exits at %s without cleaning up", name)


def disarm() -> None:
    global _armed
    _armed = None


def crash_point(name: str) -> None:
    """Exit the process at once, with CRASH_EXIT_CODE, if `name` is the armed crash point."""
    if name == _armed:
        os._exit(CRASH_EXIT_CODE)
