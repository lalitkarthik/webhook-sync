"""Rejection counters: counted in memory per source and reason, written once a minute (Q84, A2).

A flood of junk therefore costs one write a minute, not one per request. Each minute becomes one
`rejection_counts` row per source and reason, written once the minute has closed. `write_all`
writes the open minute too: the shutdown calls it once more after serving stops. No samples in P1.
"""

from collections import Counter
from datetime import datetime

from sqlalchemy.dialects import sqlite

from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.db.schema import rejection_counts
from webhook_sync.db.writer import Writer

type _Key = tuple[str, str, str]
"""(source, reason, minute as stored text)."""


class RejectionCounters:
    def __init__(self) -> None:
        self._counts: Counter[_Key] = Counter()

    def count(self, source: str, reason: str) -> None:
        """Count one rejection in the current minute."""
        self._counts[(source, reason, _minute(utc_now()))] += 1

    def has_closed_minutes(self) -> bool:
        current = _minute(utc_now())
        return any(minute < current for _, _, minute in self._counts)

    async def write_closed_minutes(self, writer: Writer) -> None:
        """Write every minute before the current one, in one transaction."""
        current = _minute(utc_now())
        await self._write(writer, [key for key in self._counts if key[2] < current])

    async def write_all(self, writer: Writer) -> None:
        """Write every count, the open minute's included (at shutdown)."""
        await self._write(writer, list(self._counts))

    async def _write(self, writer: Writer, keys: list[_Key]) -> None:
        if not keys:
            return
        # Taken out before the write, so rejections counted meanwhile aren't lost or written twice.
        taken = Counter({key: self._counts.pop(key) for key in keys})
        try:
            async with writer.transaction() as tx:
                for (source, reason, minute), count in taken.items():
                    statement = sqlite.insert(rejection_counts).values(
                        source=source, reason=reason, minute=minute, count=count
                    )
                    # A minute written at shutdown and again after a quick restart adds up.
                    await tx.execute(
                        statement.on_conflict_do_update(
                            index_elements=["source", "reason", "minute"],
                            set_={"count": rejection_counts.c.count + statement.excluded.count},
                        )
                    )
        except BaseException:
            self._counts.update(taken)  # kept for the next write
            raise


def _minute(moment: datetime) -> str:
    return to_db_text(moment.replace(second=0, microsecond=0))
