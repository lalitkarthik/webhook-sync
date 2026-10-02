"""The lock file: one server per database (ADR 0007).

An operating-system lock on `<data_dir>/webhook-sync.lock`, never a soft (file-exists) lock, so
the OS frees it when the process dies, even by SIGKILL (A12, G12). A 2 s timeout covers the lag
in which Windows releases a dead process's lock.
"""

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from webhook_sync.db.files import lock_path

LOCK_TIMEOUT_SECONDS = 2.0


class LockHeld(Exception):
    """Another server holds the database's lock file."""


@contextmanager
def hold_lock(data_dir: Path) -> Generator[None]:
    """Hold the lock on `data_dir`'s database until the block ends; raise LockHeld if taken."""
    path = lock_path(data_dir)
    lock = FileLock(path, timeout=LOCK_TIMEOUT_SECONDS, fallback_to_soft=False)
    try:
        lock.acquire()
    except Timeout:
        raise LockHeld(
            f"another wsync serve holds the lock file {path}: one server per database"
        ) from None
    try:
        yield
    finally:
        lock.release()
