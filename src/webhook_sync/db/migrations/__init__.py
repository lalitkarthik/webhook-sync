"""The migration runner: numbered `NNNN_<name>.sqlite.sql` files, applied in order at start.

Each pending file runs in one transaction with its `schema_version` row (A9). History only moves
forward: a file is never edited once released, and a later change is a new file.
"""

import re
import sqlite3
from dataclasses import dataclass
from importlib import resources

from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.db.writer import Writer

_FILE_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sqlite\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def known_migrations() -> list[Migration]:
    """Every migration this code ships, in version order."""
    found: list[Migration] = []
    for entry in resources.files(__package__).iterdir():
        match = _FILE_NAME.match(entry.name)
        if match:
            found.append(Migration(int(match[1]), entry.name, entry.read_text(encoding="utf-8")))
    return sorted(found, key=lambda m: m.version)


async def current_version(writer: Writer) -> int:
    """The highest applied version, or 0 for an empty database."""
    async with writer.transaction() as tx:
        if not await tx.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ):
            return 0
        rows = await tx.execute("SELECT max(version) AS version FROM schema_version")
        return rows[0]["version"] or 0


async def apply_pending(writer: Writer) -> list[int]:
    """Apply every migration newer than the database, each in its own transaction.

    Returns the versions applied (empty when the database is current).
    """
    applied: list[int] = []
    current = await current_version(writer)
    for migration in known_migrations():
        if migration.version <= current:
            continue
        async with writer.transaction() as tx:
            for statement in _statements(migration.sql):
                await tx.execute(statement)
            await tx.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (:version, :now)",
                {"version": migration.version, "now": to_db_text(utc_now())},
            )
        applied.append(migration.version)
    return applied


def _statements(script: str) -> list[str]:
    """Split a script into complete statements (executescript would commit our transaction)."""
    statements: list[str] = []
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statements.append(pending.strip())
            pending = ""
    leftover = [
        line for line in pending.splitlines() if line.strip() and not line.strip().startswith("--")
    ]
    if leftover:
        raise ValueError("migration ends with an incomplete statement")
    return statements
