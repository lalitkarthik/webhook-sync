"""The writer: the one connection every write goes through, one transaction at a time.

One aiosqlite connection in manual-transaction mode behind an async lock; the writer issues
`BEGIN IMMEDIATE` itself, so writes queue in our code, in order (ADR 0004, Q83, G11). WAL and
`synchronous=FULL`: a commit is on disk before the receiver answers.
"""

import asyncio
import sqlite3
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Self

import aiosqlite

from webhook_sync.db.files import database_path
from webhook_sync.db.sql import Statement, compile_statement


class Transaction:
    """The open transaction handed out by `Writer.transaction()`."""

    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    async def execute(
        self, statement: Statement, parameters: Mapping[str, Any] | None = None
    ) -> list[sqlite3.Row]:
        """Run one statement and return its rows (a RETURNING clause's, or a SELECT's)."""
        sql, compiled = compile_statement(statement)
        cursor = await self._connection.execute(sql, {**compiled, **(parameters or {})})
        rows = await cursor.fetchall()
        await cursor.close()
        return list(rows)  # pyright: ignore[reportReturnType]  # row_factory is sqlite3.Row


class Writer:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, data_dir: Path) -> Self:
        """Open (creating if needed) the database under `data_dir` for writing."""
        connection = await aiosqlite.connect(
            database_path(data_dir),
            isolation_level=None,
            autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL,
        )
        connection.row_factory = sqlite3.Row
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA synchronous=FULL",
            "PRAGMA busy_timeout=5000",
            "PRAGMA foreign_keys=ON",
        ):
            await connection.execute(pragma)
        return cls(connection)

    async def close(self) -> None:
        async with self._lock:
            await self._connection.close()

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[Transaction]:
        """`BEGIN IMMEDIATE` … `COMMIT`, or `ROLLBACK` if the block raises."""
        async with self._lock:
            await self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield Transaction(self._connection)
            except BaseException:
                await self._connection.execute("ROLLBACK")
                raise
            await self._connection.execute("COMMIT")
