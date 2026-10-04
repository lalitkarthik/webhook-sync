"""The sweeper: Razorpay's payments and refunds lists walked in time slices (sweeper.md).

Each list has its own checkpoint, on Razorpay's clock (A16): every item created before it has
been listed and stored at least once. Each walk first reads Razorpay's now from the `Date` of a
one-second list call, then from each slice's answer. A list with no checkpoint gets one at once,
`initial_lookback_days` before that now (Q157); `last_sweep_at` stays NULL until its first slice
is stored. A slice runs from the checkpoint to at most 6 h later, never past Razorpay's now -
60 s, and is requested up to its end, so payments created meanwhile can't shift its pages. It is
requested from 10 minutes before its start, so the overlap re-reads the edge; a list's first
slice has no edge to re-read and keeps nothing older than its start, so a lookback of 0 starts
empty. Only items created before a slice's end are kept. Each kept item is stored as a
synthetic event if absent (`sweep:<entity id>:<state hash>`), a new one with its ledger job;
then, in its own transaction, the checkpoint moves to the slice's end. An empty slice moves it
too, so a quiet account's checkpoint keeps up. A sweep stopped mid-slice re-reads that slice
next time, and insert-if-absent stores nothing twice.

Each list is also re-listed hourly over its last 7 days (payments) or 21 days (refunds), never
before its first checkpoint, so a later change to an item the slices have passed (a capture, a
fee, a refund reaching processed) is a new state hash, so a new event (Q108). Backfill lists
payments created on past local dates as `backfill:` events and never touches a checkpoint
(Q156).

The app's lifespan runs `run`: one sweep after a random 0-30 s delay, then one every 10 minutes,
skipped while Razorpay is paused. A pass that stored every due slice, and each re-list when due,
POSTs the heartbeat URL when one is set (Q148).
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from webhook_sync.alerts import Alerter
from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.config import Config
from webhook_sync.db.schema import checkpoints, events
from webhook_sync.db.writer import Writer
from webhook_sync.queue import Failure, Pauses
from webhook_sync.receiver.store import store_event
from webhook_sync.sources.razorpay import Listed, ListedEntity, RazorpayAdapter, SweptOrigin

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
RELIST_EVERY = timedelta(hours=1)
"""How often each list is re-listed whole over its re-list window."""
RELIST_WINDOW: dict[ListName, timedelta] = {
    "payments": timedelta(days=7),
    "refunds": timedelta(days=21),
}
"""How far back the re-list reads each list, by creation time (Q108)."""
SILENT_AGE = timedelta(minutes=10)
"""A swept payment created longer ago than this, on Razorpay's clock, that no delivery brought
means the webhook may have stopped (Q149)."""
SILENT_QUIET = timedelta(hours=24)
"""No delivery for this long while slices store events means the same."""
NAMED_IN_AN_ALERT = 10
"""At most this many payment ids are named in a `silent_webhook` alert."""
HEARTBEAT_TIMEOUT = 10.0
"""Seconds the heartbeat URL gets to answer."""

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
        alerter: Alerter | None = None,
        rng: random.Random | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._writer = writer
        self._razorpay = razorpay
        self._config = config
        self._pauses = pauses
        self._alerter = alerter
        self._rng = rng or random.Random()
        self._sleep = sleep
        self._sweeping = asyncio.Lock()
        """One sweep at a time: a sweep asked for by hand waits for the loop's, and the other
        way round, so no checkpoint moves back."""

    async def run(self) -> None:
        """Sweep after a random 0-30 s delay, then every 10 minutes, until cancelled. A sweep
        that raises is logged; the next one runs on time."""
        await self._sleep(self._rng.uniform(0, START_DELAY.total_seconds()))
        while True:
            try:
                finished = await self.sweep()
                if finished:
                    await self._heartbeat()
                log.info("sweep pass ended", extra={"finished": finished})
            except Exception:
                log.exception("sweep failed; the next one runs on time")
            await self._sleep(SWEEP_EVERY.total_seconds())

    async def _heartbeat(self) -> None:
        """POST the heartbeat URL after a healthy pass (Q148): one try, 10 s, a failure
        logged. No URL, no ping."""
        url = self._config.sweeper.heartbeat_url
        if not url:
            return
        try:
            async with httpx.AsyncClient(timeout=HEARTBEAT_TIMEOUT) as client:
                answer = await client.post(url)
            answer.raise_for_status()
        except httpx.HTTPError as error:
            status = (
                error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            )
            log.warning(
                "heartbeat not sent: %s", type(error).__name__, extra={"http_status": status}
            )
            return
        log.info("heartbeat sent")

    async def sweep(self) -> bool:
        """One sweep of both lists; True when every due slice of each was stored, and its
        re-list when due. Nothing is listed while Razorpay is paused."""
        async with self._sweeping:
            return await self._sweep()

    async def _sweep(self) -> bool:
        finished = True
        stored = 0
        unannounced: list[str] = []
        try:
            for list_name, entity in LISTS:
                if self._razorpay.name in self._pauses.sources:
                    log.info("sweep skipped: Razorpay is paused", extra={"list": list_name})
                    return False
                walked = await self._walk(list_name, entity)
                if walked is None:
                    finished = False
                    continue
                stored += walked.stored
                unannounced.extend(walked.unannounced)
                floor = await self._relist_floor(list_name)
                if floor is not None:
                    finished = (
                        await self._relist(list_name, entity, walked.source_now, floor) and finished
                    )
        finally:
            await self._check_silence(stored, unannounced)
        return finished

    async def backfill(self, first: date, last: date) -> int | Failure:
        """Store every payment created on the local dates `first` to `last` (`service.timezone`,
        A20) as a `backfill:` synthetic event with its fetching ledger job; the count of new
        events. No checkpoint moves (Q156). Nothing is listed while Razorpay is paused."""
        if self._razorpay.name in self._pauses.sources:
            return Failure("Razorpay is paused", "pause")
        zone = self._config.service.timezone
        start = _seconds_of(datetime.combine(first, time(), zone))
        end = _seconds_of(datetime.combine(last + timedelta(days=1), time(), zone)) - 1
        listed = await self._list("payments", start, end)
        if isinstance(listed, Failure):
            return listed
        new = len(await self._store("payment", listed.items, origin="backfill"))
        log.info(
            "backfill",
            extra={
                "from": first.isoformat(),
                "to": last.isoformat(),
                "listed": len(listed.items),
                "stored": new,
            },
        )
        return new

    async def _walk(self, list_name: ListName, entity: ListedEntity) -> _Walked | None:
        """Walk one list's slices from its checkpoint up to Razorpay's now - 60 s.

        Razorpay's now is read first, from a one-second list call, then from each slice's
        answer: every slice is requested up to its own end, never past Razorpay's now - 60 s,
        so payments created during the walk can't shift its pages (razorpay-list-facts.md).
        Razorpay's now at the end of the walk, with what it stored; None when a list call
        failed."""
        probe = await self._source_now(list_name)
        if isinstance(probe, Failure):
            return _stopped(list_name, probe)
        source_now = probe
        start, first, history = await self._checkpoint(list_name, source_now)
        stored = 0
        unannounced: list[str] = []
        while True:
            upper = start + _seconds(SLICE)
            end = min(upper, source_now - _seconds(SETTLE))
            if end <= start:
                break  # the checkpoint is already at Razorpay's now - 60 s
            floor = start if first else start - _seconds(OVERLAP)
            listed = await self._list(list_name, floor, end)
            if isinstance(listed, Failure):
                return _stopped(list_name, listed)
            source_now = _seconds_of(listed.source_now)
            kept = [item for item in listed.items if floor <= item["created_at"] < end]
            new = await self._store(entity, kept, origin="sweep")
            if entity == "payment":
                # Created before the first start: the lookback's history, never delivered.
                old = source_now - _seconds(SILENT_AGE)
                unannounced.extend(
                    item["id"] for item in new if history <= item["created_at"] < old
                )
            await self._advance(list_name, end)
            log.info(
                "sweep slice",
                extra={
                    "list": list_name,
                    "slice_start": to_db_text(_moment(start)),
                    "slice_end": to_db_text(_moment(end)),
                    "listed": len(kept),
                    "stored": len(new),
                },
            )
            stored += len(new)
            if end < upper:
                break  # caught up with Razorpay's now
            start, first = end, False
        await self._swept(list_name, stored)
        return _Walked(source_now, stored, unannounced)

    async def _relist_floor(self, list_name: ListName) -> int | None:
        """The list's re-list floor in Unix seconds when its re-list is due (last run an hour
        ago or more, on our clock); None when it isn't."""
        async with self._writer.transaction() as tx:
            rows = await tx.execute(
                sa.select(checkpoints.c.last_relist_at, checkpoints.c.relist_floor_at).where(
                    *_matching(self._key(list_name))
                )
            )
        last = rows[0]["last_relist_at"]
        if last is not None and last > to_db_text(utc_now() - RELIST_EVERY):
            return None
        return int(datetime.fromisoformat(rows[0]["relist_floor_at"]).timestamp())

    async def _relist(
        self, list_name: ListName, entity: ListedEntity, source_now: int, floor: int
    ) -> bool:
        """Re-list every item created in the list's re-list window up to Razorpay's now, never
        before its floor (the first checkpoint); an item whose state hash is new is a new
        synthetic event with its ledger job (Q108)."""
        start = max(source_now - _seconds(RELIST_WINDOW[list_name]), floor)
        listed = await self._list(list_name, start, source_now)
        if isinstance(listed, Failure):
            _stopped(list_name, listed)
            return False
        new = len(await self._store(entity, listed.items, origin="sweep"))
        async with self._writer.transaction() as tx:
            await tx.execute(
                sa.update(checkpoints)
                .where(*_matching(self._key(list_name)))
                .values(last_relist_at=to_db_text(utc_now()))
            )
        log.info("re-list", extra={"list": list_name, "listed": len(listed.items), "stored": new})
        return True

    async def _source_now(self, list_name: ListName) -> int | Failure:
        """Razorpay's now in Unix seconds, from the `Date` of a one-second list call."""
        now = _seconds_of(utc_now())
        listed = await self._list(list_name, now, now)
        return listed if isinstance(listed, Failure) else _seconds_of(listed.source_now)

    async def _list(self, list_name: ListName, start: int, end: int) -> Listed | Failure:
        lister = (
            self._razorpay.list_payments if list_name == "payments" else self._razorpay.list_refunds
        )
        return await lister(start, end, writer=self._writer, pauses=self._pauses)

    async def _store(
        self, entity: ListedEntity, items: Sequence[Mapping[str, Any]], *, origin: SweptOrigin
    ) -> list[Mapping[str, Any]]:
        """Store each item's synthetic event if absent, all in one transaction; the items
        whose event was new."""
        configured = self._config.razorpay
        new: list[Mapping[str, Any]] = []
        now = utc_now()
        async with self._writer.transaction() as tx:
            for item in items:
                described = self._razorpay.describe_listed(entity, item, origin=origin)
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
                    origin=origin,
                    body_sha256=None,
                    received_at=now,
                )
                if stored is not None:
                    new.append(item)
        return new

    async def _check_silence(self, stored: int, unannounced: Sequence[str]) -> None:
        """The silent-webhook check after a pass (Q149): an alert when the slices stored a
        payment more than 10 minutes old that no delivery brought, or stored anything while no
        delivery has arrived for 24 h (counted from the first stored event when none ever
        did). Payments created before the first start are the lookback's history and count
        for neither rule's old payments."""
        if self._alerter is None or not stored:
            return
        ours = (
            events.c.source == self._razorpay.name,
            events.c.account == self._config.razorpay.account,
        )
        delivered = sa.and_(*ours, events.c.origin == "delivery")
        async with self._writer.transaction() as tx:
            announced = await tx.execute(
                sa.select(events.c.payment_key)
                .distinct()
                .where(delivered, events.c.payment_key.in_(unannounced))
            )
            last = await tx.execute(
                sa.select(sa.func.max(events.c.received_at).label("at")).where(delivered)
            )
            first = await tx.execute(
                sa.select(sa.func.min(events.c.received_at).label("at")).where(*ours)
            )
        silent = sorted(set(unannounced) - {row["payment_key"] for row in announced})
        quiet_since = last[0]["at"] or first[0]["at"]
        found: list[str] = []
        if silent:
            named = ", ".join(silent[:NAMED_IN_AN_ALERT])
            if len(silent) > NAMED_IN_AN_ALERT:
                named += ", ..."
            found.append(
                f"{len(silent)} payment(s) more than 10 minutes old that no delivery brought"
                f" ({named})"
            )
        if quiet_since is not None and quiet_since <= to_db_text(utc_now() - SILENT_QUIET):
            found.append(f"no delivery for 24 hours while sweeps stored {stored} event(s)")
        if found:
            log.warning("events reaching us only through the sweeper", extra={"stored": stored})
            await self._alerter.silent_webhook("; ".join(found))

    async def _checkpoint(self, list_name: ListName, source_now: int) -> tuple[int, bool, int]:
        """The list's checkpoint in Unix seconds, whether it is still the first start's (no
        slice stored yet), and Razorpay's now at the first start (`first_start_at`): what was
        created before it is the lookback's history, however many passes the walk takes. With
        none, one is stored at once, `initial_lookback_days` before Razorpay's now, so a restart
        doesn't move it. The re-list never reads before this first checkpoint
        (`relist_floor_at`), so a first start reads only its lookback (Q157), and it is first
        due an hour on."""
        key = self._key(list_name)
        lookback = timedelta(days=self._config.sweeper.initial_lookback_days)
        first = _moment(source_now - _seconds(lookback))
        async with self._writer.transaction() as tx:
            await tx.execute(
                sqlite.insert(checkpoints)
                .values(
                    **key,
                    checkpoint_at=to_db_text(first),
                    last_sweep_stored=0,
                    last_relist_at=to_db_text(utc_now()),
                    relist_floor_at=to_db_text(first),
                    first_start_at=to_db_text(_moment(source_now)),
                )
                .on_conflict_do_nothing()
            )
        stored = await self._stored_checkpoint(key)
        assert stored is not None
        return stored

    async def _stored_checkpoint(self, key: Mapping[str, str]) -> tuple[int, bool, int] | None:
        async with self._writer.transaction() as tx:
            rows = await tx.execute(
                sa.select(
                    checkpoints.c.checkpoint_at,
                    checkpoints.c.last_sweep_at,
                    checkpoints.c.first_start_at,
                ).where(*_matching(key))
            )
        if not rows:
            return None
        row = rows[0]
        return (
            _seconds_of(datetime.fromisoformat(row["checkpoint_at"])),
            row["last_sweep_at"] is None,
            _seconds_of(datetime.fromisoformat(row["first_start_at"])),
        )

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


@dataclass(frozen=True)
class _Walked:
    """A finished walk of one list: Razorpay's now at its end, how many events its slices
    stored, and the payments among them more than 10 minutes old (outside the first start's
    lookback)."""

    source_now: int
    stored: int
    unannounced: list[str]


def _matching(key: Mapping[str, str]) -> list[sa.ColumnElement[bool]]:
    return [checkpoints.c[name] == value for name, value in key.items()]


def _stopped(list_name: ListName, failure: Failure) -> None:
    log.warning(
        "sweep stopped: the list failed",
        extra={"list": list_name, "error": failure.error, "error_class": failure.error_class},
    )


def _seconds_of(moment: datetime) -> int:
    return int(moment.timestamp())


def _seconds(span: timedelta) -> int:
    return int(span.total_seconds())


def _moment(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)
