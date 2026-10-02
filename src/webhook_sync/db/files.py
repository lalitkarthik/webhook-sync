"""Where the database's files live: all under the data directory (A10)."""

from pathlib import Path

DATABASE_FILE = "webhook-sync.db"
LOCK_FILE = "webhook-sync.lock"


def database_path(data_dir: Path) -> Path:
    return data_dir / DATABASE_FILE


def lock_path(data_dir: Path) -> Path:
    return data_dir / LOCK_FILE
