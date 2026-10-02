"""SQLAlchemy Core builds the statements; aiosqlite runs them (A15)."""

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

_DIALECT = sqlite.dialect(paramstyle="named")

type Statement = sa.ClauseElement | str


def compile_statement(statement: Statement) -> tuple[str, dict[str, Any]]:
    """The SQL text and named parameters for a Core statement, or a plain SQL string as is."""
    if isinstance(statement, str):
        return statement, {}
    compiled = statement.compile(dialect=_DIALECT, compile_kwargs={"render_postcompile": True})
    return str(compiled), dict(compiled.construct_params() or {})
