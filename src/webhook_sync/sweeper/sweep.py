"""The sweeper: Razorpay's payments and refunds lists walked in time slices (sweeper.md).

Each list has its own checkpoint, on Razorpay's clock (A16): every item created before it has
been listed and stored at least once. A list with no checkpoint gets one at once,
`initial_lookback_days` before Razorpay's now (Q157), read from the `Date` of a one-second list
call; `last_sweep_at` stays NULL until its first slice is stored. A slice runs from the checkpoint
to at most 6 h later, and never past Razorpay's now - 60 s, read from the answer's `Date` header;
it is requested from 10 minutes before its start, so the overlap re-reads the edge, and only items
created before its end are kept. A list's first slice has no edge to re-read: it is requested
from its start and keeps nothing older, so a lookback of 0 starts empty. Each
kept item is stored as a synthetic event if absent (`sweep:<entity id>:<state hash>`), a new one
with its ledger job; then, in its own transaction, the checkpoint moves to the slice's end. An
empty slice moves it too, so a quiet account's checkpoint keeps up. A sweep stopped mid-slice
re-reads that slice next time, and insert-if-absent stores nothing twice.

The app's lifespan runs `run`: one sweep after a random 0-30 s delay, then one every 10 minutes,
skipped while Razorpay is paused.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.config import Config
from webhook_sync.db.schema import checkpoints
from webhook_sync.db.writer import Writer
from webhook_sync.queue import Failure, Pauses
from webhook_sync.receiver.store import store_event
from webhook_sync.sources.razorpay import Listed, ListedEntity, RazorpayAdapter

log = logging.getLogger("webhook_sync.sweeper")

type ListName = Literal["payments", "refunds"]
"""A checkpoint's list: Razorpay's payments list or its refunds list."""

LISTS: tuple[tuple[ListName, ListedEntity], ...] = (("payments", "payment"), ("refunds", "refund"))

SLICE = timedelta(hours=6)
"""The longest time slice."""
OVERLAP = timedelta(minutes=10)
"""Re-read before each slice's start: an item can become listable a little after its
`created_at`."""
SETTLE = timedelta(seconds=60)
"""How far behind Razorpay's now a slice ends, for the same reason."""
START_DELAY = timedelta(seconds=30)
"""The first sweep waits a random time up to this after start."""
SWEEP_EVERY = timedelta(minutes=10)
"""The wait between sweeps."""

type Sleep = Callable[[float], Awaitable[None]]


class Sweeper:
    """Razorpay's sweeper for the configured account, storing through the shared event store."""

    def __init__(
        self,
        writer: Writer,
        razorpay: RazorpayAdapter,
        config: Config,
        *,
        pauses: Pauses,
        rng: random.Random | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._writer = writer
        self._razorpay = razorpay
        self._config = config
        self._pauses = pauses
        self._rng = rng or random.Random()
        self._sleep = sleep

    async def run(self) -> None:
        """Sweep after a random 0-30 s delay, then every 10 minutes, until cancelled. A sweep
        that raises is logged; the next one runs on time."""
        await self._sleep(self._rng.uniform(0, START_DELAY.total_seconds()))
        while True:
            try:
                await self.sweep()
            except Exception:
                log.exception("sweep failed; the next one runs on time")
            await self._sleep(SWEEP_EVERY.total_seconds())

    async def sweep(self) -> bool:
        """One sweep of both lists; True when every due slice of each was stored. Nothing is
        listed while Razorpay is paused."""
        finished = True
        for list_name, entity in LISTS:
            if self._razorpay.name in self._pauses.sources:
                log.info("sweep skipped: Razorpay is paused", extra={"list": list_name})
                return False
            finished = await self._walk(list_name, entity) and finished
        return finished

    async def _walk(self, list_name: ListName, entity: ListedEntity) -> bool:
        """Walk one list's slices from its checkpoint up to Razorpay's now - 60 s."""
        checkpoint = await self._checkpoint(list_name)
        if isinstance(checkpoint, Failure):
            return _stopped(list_name, checkpoint)
        start, first = checkpoint
        stored = 0
        while True:
            upper = start + _seconds(SLICE)
            floor = start if first else start - _seconds(OVERLAP)
            listed = await self._list(list_name, floor, upper)
            if isinstance(listed, Failure):
                return _stopped(list_name, listed)
            end = min(upper, _seconds_of(listed.source_now) - _seconds(SETTLE))
            if end <= start:
                break  # the checkpoint is already at Razorpay's now - 60 s
            kept = [item for item in listed.items if floor <= item["created_at"] < end]
            new = await self._store(entity, kept)
            await self._advance(list_name, end)
            log.info(
                "sweep slice",
                extra={
                    "list": list_name,
                    "slice_start": to_db_text(_moment(start)),
                    "slice_end": to_db_text(_moment(end)),
                    "listed": len(kept),
                    "stored": new,
                },
            )
            stored += new
            if end < upper:
                break  # caught up with Razorpay's now
            start, first = end, False
        await self._swept(list_name, stored)
        return True

    async def _list(self, list_name: ListName, start: int, end: int) -> Listed | Failure:
        lister = (
            self._razorpay.list_payments if list_name == "payments" else self._razorpay.list_refunds
        )
        return await lister(start, end, writer=self._writer, pauses=self._pauses)

    async def _store(self, entity: ListedEntity, items: Sequence[Mapping[str, Any]]) -> int:
        """Store each item's synthetic event if absent, all in one transaction; the count of
        new events."""
        configured = self._config.razorpay
        new = 0
        now = utc_now()
        async with self._writer.transaction() as tx:
            for item in items:
                described = self._razorpay.describe_listed(entity, item, origin="sweep")
                if described is None:
                    continue  # a `created` payment: nothing attempted yet
                event_id, description = described
                stored = await store_event(
                    tx,
                    source=self._razorpay.name,
                    account=configured.account,
                    mode=configured.mode,
                    retry_profile=self._config.service.retry_profile,
                    event_id=event_id,
                    description=description,
                    origin="sweep",
                    body_sha256=None,
                    received_at=now,
                )
                new += stored is not None
        return new

    async def _checkpoint(self, list_name: ListName) -> tuple[int, bool] | Failure:
        """The list's checkpoint in Unix seconds, and whether it is still the first start's (no
        slice stored yet). With none, one is stored at once, `initial_lookback_days` before
        Razorpay's now, so a restart doesn't move it."""
        key = self._key(list_name)
        stored = await self._stored_checkpoint(key)
        if stored is not None:
            return stored
        now = int(utc_now().timestamp())
        listed = await self._list(list_name, now, now)  # only its `Date` is wanted
        if isinstance(listed, Failure):
            return listed
        lookback = timedelta(days=self._config.sweeper.initial_lookback_days)
        first = _moment(_seconds_of(listed.source_now) - _seconds(lookback))
        async with self._writer.transaction() as tx:
            await tx.execute(
                sqlite.insert(checkpoints)
                .values(**key, checkpoint_at=to_db_text(first), last_sweep_stored=0)
                .on_conflict_do_nothing()
            )
        stored = await self._stored_checkpoint(key)
        assert stored is not None
        return stored

    async def _stored_checkpoint(self, key: Mapping[str, str]) -> tuple[int, bool] | None:
        async with self._writer.transaction() as tx:
            rows = await tx.execute(
                sa.select(checkpoints.c.checkpoint_at, checkpoints.c.last_sweep_at).where(
                    *_matching(key)
                )
            )
        if not rows:
            return None
        at = int(datetime.fromisoformat(rows[0]["checkpoint_at"]).timestamp())
        return at, rows[0]["last_sweep_at"] is None

    async def _advance(self, list_name: ListName, end: int) -> None:
        """Move the checkpoint to a stored slice's end; `last_sweep_at` is when it last moved."""
        async with self._writer.transaction() as tx:
            await tx.execute(
                sa.update(checkpoints)
                .where(*_matching(self._key(list_name)))
                .values(checkpoint_at=to_db_text(_moment(end)), last_sweep_at=to_db_text(utc_now()))
            )

    async def _swept(self, list_name: ListName, stored: int) -> None:
        async with self._writer.transaction() as tx:
            await tx.execute(
                sa.update(checkpoints)
                .where(*_matching(self._key(list_name)))
                .values(last_sweep_stored=stored)
            )

    def _key(self, list_name: ListName) -> dict[str, str]:
        return {
            "source": self._razorpay.name,
            "account": self._config.razorpay.account,
            "list": list_name,
        }


def _matching(key: Mapping[str, str]) -> list[sa.ColumnElement[bool]]:
    return [checkpoints.c[name] == value for name, value in key.items()]


def _stopped(list_name: ListName, failure: Failure) -> bool:
    log.warning(
        "sweep stopped: the list failed",
        extra={"list": list_name, "error": failure.error, "error_class": failure.error_class},
    )
    return False


def _seconds_of(moment: datetime) -> int:
    return int(moment.timestamp())


def _seconds(span: timedelta) -> int:
    return int(span.total_seconds())


def _moment(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)
