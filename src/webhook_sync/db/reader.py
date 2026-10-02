"""The read-only connection (`mode=ro`): reads never take the write lock."""

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from webhook_sync.db.files import database_path
from webhook_sync.db.sql import Statement, compile_statement


class Reader:
    def __init__(self, connection: aiosqlite.Connection) -> None:
        self._connection = connection

    @classmethod
    @asynccontextmanager
    async def open(cls, data_dir: Path) -> AsyncGenerator[Reader]:
        """A read-only connection to the database under `data_dir`, closed on exit."""
        uri = database_path(data_dir).resolve().as_uri() + "?mode=ro"
        async with aiosqlite.connect(uri, uri=True) as connection:
            connection.row_factory = aiosqlite.Row
            yield cls(connection)

    async def fetch_all(
        self, statement: Statement, parameters: Mapping[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        sql, compiled = compile_statement(statement)
        async with self._connection.execute(sql, {**compiled, **(parameters or {})}) as cursor:
            return [dict(row) for row in await cursor.fetchall()]
