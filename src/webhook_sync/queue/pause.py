"""Paused sources and sinks, kept in `sinks` until `wsync resume` clears them (A3, Q134).

`claim` hands out nothing of a paused source or sink. A `pause`-class failure writes the pause
to `sinks` (state, reason, since) in the transaction that fails the job, then sets it in the
in-memory `Pauses`, the claim-time cache: loaded from `sinks` at start and updated with every
pause and resume (A13). Probes and automatic resume come later.
"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from webhook_sync.clock import to_db_text
from webhook_sync.db.schema import sinks
from webhook_sync.db.writer import Transaction, Writer

type PauseTarget = Literal["source", "sink"]
"""What a `pause`-class error stops: the source it read from, or the sink it wrote to."""

type PauseAlert = Callable[[PauseTarget, str, str], Awaitable[None]]
"""Told of every new pause: what it stops, its name and the reason (the `paused` alert)."""

type Resumed = Literal["resumed", "active", "unknown"]
"""What `resume` found: a pause it cleared, a name not paused, or a name `sinks` doesn't hold."""


class Pauses:
    """The claim-time cache of what is paused."""

    def __init__(self, *, on_pause: PauseAlert | None = None) -> None:
        self._sources: set[str] = set()
        self.on_pause = on_pause
        """Awaited after a pause newly stored in `sinks` is set here; never with one already
        in force."""
        self._sinks: set[str] = set()

    def pause(self, target: PauseTarget, name: str) -> None:
        (self._sources if target == "source" else self._sinks).add(name)

    def resume(self, name: str) -> None:
        self._sources.discard(name)
        self._sinks.discard(name)

    @property
    def sources(self) -> frozenset[str]:
        return frozenset(self._sources)

    @property
    def sinks(self) -> frozenset[str]:
        return frozenset(self._sinks)


async def record_pause(
    tx: Transaction, target: PauseTarget, name: str, reason: str, *, now: datetime
) -> bool:
    """Store a pause in `sinks`. A name already paused keeps its first reason and time.
    Returns whether the pause is new."""
    insert = sqlite.insert(sinks).values(
        name=name, kind=target, state="paused", reason=reason, since=to_db_text(now)
    )
    stored = await tx.execute(
        insert.on_conflict_do_update(
            index_elements=[sinks.c.name],
            set_={
                "state": "paused",
                "reason": insert.excluded.reason,
                "since": insert.excluded.since,
            },
            where=sinks.c.state != "paused",
        ).returning(sinks.c.name)
    )
    return bool(stored)


async def register(writer: Writer, names: dict[str, PauseTarget]) -> None:
    """Give every configured source and sink a `sinks` row, active unless one exists."""
    if not names:
        return
    async with writer.transaction() as tx:
        await tx.execute(
            sqlite.insert(sinks)
            .values([{"name": n, "kind": k, "state": "active"} for n, k in names.items()])
            .on_conflict_do_nothing(index_elements=[sinks.c.name])
        )


async def load_pauses(writer: Writer) -> Pauses:
    """The claim-time cache, as `sinks` holds it: every source and sink paused there."""
    pauses = Pauses()
    async with writer.transaction() as tx:
        rows = await tx.execute(
            sa.select(sinks.c.name, sinks.c.kind).where(sinks.c.state == "paused")
        )
    for row in rows:
        pauses.pause("source" if row["kind"] == "source" else "sink", row["name"])
    return pauses


async def resume(writer: Writer, name: str, *, pauses: Pauses | None, now: datetime) -> Resumed:
    """Clear `name`'s pause in `sinks` and, when a server's cache is given, in the cache."""
    async with writer.transaction() as tx:
        rows = await tx.execute(sa.select(sinks.c.state).where(sinks.c.name == name))
        if not rows:
            return "unknown"
        if rows[0]["state"] != "paused":
            return "active"
        await tx.execute(
            sa.update(sinks)
            .where(sinks.c.name == name)
            .values(state="active", reason=None, since=to_db_text(now))
        )
    if pauses is not None:
        pauses.resume(name)
    return "resumed"
