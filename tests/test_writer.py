"""The writer's transactions on real SQLite: none is ever left open for the next one."""

import sqlite3

import pytest
from support import ReadRows

from webhook_sync.db.writer import Writer


async def test_a_commit_that_fails_leaves_no_open_transaction_and_nothing_written(
    writer: Writer, read_rows: ReadRows
) -> None:
    async def write_an_attempt_of_no_job() -> None:
        async with writer.transaction() as tx:
            await tx.execute("PRAGMA defer_foreign_keys = ON")  # the check moves to COMMIT
            await tx.execute(
                "INSERT INTO attempts (job_id, number, finished_at, outcome) "
                "VALUES (999, 1, '2026-10-02T09:00:00.000000Z', 'ok')"
            )

    with pytest.raises(sqlite3.IntegrityError):
        await write_an_attempt_of_no_job()

    async with writer.transaction() as tx:
        await tx.execute("SELECT 1")

    assert await read_rows("SELECT * FROM attempts") == []
