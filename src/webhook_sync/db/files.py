"""Where the database's files live: all under the data directory (A10)."""

from pathlib import Path

DATABASE_FILE = "webhook-sync.db"


def database_path(data_dir: Path) -> Path:
    return data_dir / DATABASE_FILE
