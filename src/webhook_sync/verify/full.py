"""The full run (verify.md, Q151, Q154): Razorpay against the ledger, then the sheet against it.

The app's lifespan runs it nightly: once per local date in `service.timezone`, at the first valid
`[verify] nightly_at` at or after that time (a daylight-saving change can skip it), and at start
when today's time has passed with no full run stored for today, so a server down at 02:00 runs
it on start the same day. The first run after a start, caught up or due soon after, first waits
(2 minutes at most) for the sweeper to have walked both lists up to the server's start, so
payments made while it was down come in through the sweep, with Via = sweep, not as verify's
repairs. "Once per local date" is read from `verify_runs`, so it survives restarts (A18). A run
whose list fails, or that raises, is tried again every 10 minutes (longer when Razorpay's
`Retry-After` asks) until a full run is stored or the date is over, when one warning names the
date and its last failure. While Razorpay is paused the source half is skipped: the sheet is
checked as a quick run, and that night is done.

A full run is stored as one `verify_runs` row with the findings of both checks; with `[verify]
auto_repair` (or a repair asked for) they are repaired in the same transaction. Its line, e.g.
`Verified 03 Oct 02:00 IST: last 30 days, 7 payments, 0 differences (2 ignored events)`, goes on
Sync status through a status-only flush (Q153). Findings left unrepaired raise a
`verify_differences` alert with their counts, and a payment `stale` on this run and the full run
before it a `stale_twice` alert: something keeps undoing the repair (Q155).
"""

import asyncio
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as wall_time
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from webhook_sync.alerts import Alerter
from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.config import Config
from webhook_sync.db.schema import checkpoints, verify_findings, verify_runs
from webhook_sync.db.writer import Writer
from webhook_sync.queue import Failure, Pauses
from webhook_sync.sources.razorpay import RazorpayAdapter
from webhook_sync.verify.runs import Finding, counted, local_time, record_run
from webhook_sync.verify.sheet import SheetVerify
from webhook_sync.verify.source import RazorpayVerify

log = logging.getLogger("webhook_sync.verify")

type Sleep = Callable[[float], Awaitable[None]]

RETRY_EVERY = timedelta(minutes=10)
"""The wait before a failed nightly run is tried again, unless Razorpay asks for longer."""
FIRST_SWEEP_WAIT = timedelta(minutes=2)
"""How long the catch-up at start waits for the sweeper's first pass."""
FIRST_SWEEP_LAG = timedelta(minutes=2)
"""How far behind the server's start a checkpoint may be and still count as caught up: a pass
ends a minute behind Razorpay's now, whose clock can differ a little from ours."""
POLL_EVERY = timedelta(seconds=1)
"""How often the catch-up looks at the checkpoints while it waits."""


class FullVerify:
    """The full run for the configured account, and the sheet when there is one."""

    def __init__(
        self,
        writer: Writer,
        razorpay: RazorpayAdapter,
        config: Config,
        *,
        pauses: Pauses,
        alerter: Alerter,
        sheet: SheetVerify | None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._writer = writer
        self._razorpay = razorpay
        self._config = config
        self._pauses = pauses
        self._alerter = alerter
        self._sheet = sheet
        self._source = RazorpayVerify(writer, razorpay, config, pauses=pauses)
        self._sleep = sleep
        self._zone = config.service.timezone

    async def run_nightly(self) -> None:
        """A full run once per local date at `nightly_at`, caught up at start, until
        cancelled. A pass that raises is logged and the loop carries on."""
        server_started = utc_now()
        done: date | None = None  # a night run with Razorpay paused stores no full run
        failing: tuple[date, str] | None = None  # a night still to store, and why it failed
        at_start = True
        while True:
            try:
                now = utc_now()
                night = now.astimezone(self._zone).date()
                if failing is not None and failing[0] < night:
                    log.warning(
                        "nightly verify given up: its date is over",
                        extra={"local_date": failing[0].isoformat(), "error": failing[1]},
                    )
                    failing = None
                if night == done or await self._ran_on(night):
                    night += timedelta(days=1)
                due = nightly_due(night, self._config.verify.nightly_at, self._zone)
                if due > now:
                    log.info(
                        "nightly verify waiting",
                        extra={"local_date": night.isoformat(), "due_at": to_db_text(due)},
                    )
                    await self._sleep((due - now).total_seconds())
                if at_start:  # caught up at start, or due soon after it: the sweep goes first
                    await self._first_sweep(server_started)
                at_start = False
                failed = await self._try(night)
                if failed is None:
                    done, failing = night, None
                else:
                    retry_in, error = failed
                    failing = (night, error)
                    log.warning(
                        "nightly verify failed; trying again",
                        extra={"local_date": night.isoformat(), "retry_in_s": retry_in},
                    )
                    await self._sleep(retry_in)
            except Exception:  # a database error, say: the next pass tries again
                log.exception("nightly verify pass failed; trying again")
                await self._sleep(RETRY_EVERY.total_seconds())

    async def _try(self, night: date) -> tuple[float, str] | None:
        """One nightly run; None when the night is done, else the seconds to wait before
        trying again and what failed."""
        try:
            result = await self.run(local_date=night)
        except Exception as error:
            log.exception("nightly verify raised")
            return RETRY_EVERY.total_seconds(), f"{type(error).__name__}: {error}"
        if isinstance(result, Failure):
            wait = max(RETRY_EVERY, result.retry_after or timedelta(0))
            return wait.total_seconds(), result.error
        return None

    async def _first_sweep(self, server_started: datetime) -> None:
        """Wait, `FIRST_SWEEP_WAIT` at most, until both lists' checkpoints have reached the
        server's start; not while Razorpay is paused, when there is no sweep to wait for."""
        loop = asyncio.get_running_loop()
        give_up = loop.time() + FIRST_SWEEP_WAIT.total_seconds()  # real time, whatever the clock
        caught_up = to_db_text(server_started - FIRST_SWEEP_LAG)
        source, account = self._razorpay.name, self._config.razorpay.account
        while self._razorpay.name not in self._pauses.sources and loop.time() < give_up:
            async with self._writer.transaction() as tx:
                rows = await tx.execute(
                    sa.select(checkpoints.c.list).where(
                        checkpoints.c.source == source,
                        checkpoints.c.account == account,
                        checkpoints.c.checkpoint_at >= caught_up,
                    )
                )
            if len(rows) == 2:
                return
            await self._sleep(POLL_EVERY.total_seconds())

    async def run(
        self,
        *,
        repair: bool | None = None,
        days: int | None = None,
        local_date: date | None = None,
        by_hand: bool = False,
    ) -> int | Failure | None:
        """One full run: its `verify_runs` id, or the failure of a list. While Razorpay is
        paused, a quick run instead (None when there is no sheet to check). `repair` defaults
        to `[verify] auto_repair`; `days` to `[verify] days`; `local_date` (the night it is
        for) to today's. A run `by_hand` is stored as such: it is no night's run, the run
        before no nightly run, and it alerts on nothing, since its findings are printed."""
        started = utc_now()
        clock = time.perf_counter()
        if self._razorpay.name in self._pauses.sources:
            log.info("full verify: Razorpay is paused, only the sheet is checked")
            return await self._sheet.run(by_hand=by_hand) if self._sheet is not None else None
        days = self._config.verify.days if days is None else days
        source = await self._source.check(started, days)
        if isinstance(source, Failure):
            log.warning(
                "full verify stopped: a list failed",
                extra={"error": source.error, "error_class": source.error_class},
            )
            return source
        sheet = await self._sheet.check(started) if self._sheet is not None else None
        findings = source.findings + (sheet.findings if sheet is not None else [])
        repair = self._config.verify.auto_repair if repair is None else repair
        recorded = await record_run(
            self._writer,
            self._config,
            kind="full",
            started=started,
            local_date=local_date or started.astimezone(self._zone).date(),
            window_days=days,
            checked=source.checked,
            findings=findings,
            line=status_line(
                started, self._zone, days, source.checked, len(findings), source.ignored_events
            ),
            repair=repair,
            ignored_events=source.ignored_events,
            by_hand=by_hand,
        )
        if self._sheet is not None:
            self._sheet.request_flush()
        log.info(
            "verify run",
            extra={
                "kind": "full",
                "payments_checked": source.checked,
                "findings": len(findings),
                "repaired": recorded.repaired,
                "duration_ms": round((time.perf_counter() - clock) * 1000),
            },
        )
        if not by_hand:
            await self._alert(recorded.run_id, findings, repair=repair)
        return recorded.run_id

    async def _alert(self, run_id: int, findings: list[Finding], *, repair: bool) -> None:
        unrepaired = Counter(f.kind for f in findings if not (repair and f.repair is not None))
        if unrepaired:
            await self._alerter.verify_differences(unrepaired)
        stale = {f.payment_id for f in findings if f.kind == "stale" and f.payment_id}
        again = stale & await self._stale_on_the_run_before(run_id) if stale else set[str]()
        if again:
            await self._alerter.stale_twice(sorted(again))

    async def _ran_on(self, night: date) -> bool:
        async with self._writer.transaction() as tx:
            rows = await tx.execute(
                sa.select(verify_runs.c.id)
                .where(
                    verify_runs.c.kind == "full",
                    verify_runs.c.by_hand == 0,
                    verify_runs.c.local_date == night.isoformat(),
                )
                .limit(1)
            )
        return bool(rows)

    async def _stale_on_the_run_before(self, run_id: int) -> set[str]:
        """The payments found stale by the nightly full run before `run_id`."""
        before = (
            sa.select(sa.func.max(verify_runs.c.id))
            .where(
                verify_runs.c.kind == "full", verify_runs.c.by_hand == 0, verify_runs.c.id < run_id
            )
            .scalar_subquery()
        )
        async with self._writer.transaction() as tx:
            rows = await tx.execute(
                sa.select(verify_findings.c.payment_id).where(
                    verify_findings.c.run_id == before, verify_findings.c.kind == "stale"
                )
            )
        return {r["payment_id"] for r in rows}


def status_line(
    started: datetime, zone: ZoneInfo, days: int, checked: int, differences: int, ignored: int
) -> str:
    """The full run's Sync status line (Q153); the ignored events only when there are some."""
    line = (
        f"Verified {local_time(started, zone)}: last {counted(days, 'day')}, "
        f"{counted(checked, 'payment')}, {counted(differences, 'difference')}"
    )
    return f"{line} ({counted(ignored, 'ignored event')})" if ignored else line


def nightly_due(night: date, at: wall_time, zone: ZoneInfo) -> datetime:
    """The first valid local time at or after `at` on `night` in `zone`, in UTC: a wall time a
    daylight-saving change skipped moves on to the first minute that exists; one it repeats is
    its first occurrence."""
    local = datetime.combine(night, at)
    while _round_trip(local, zone) != local:
        local += timedelta(minutes=1)
    return local.replace(tzinfo=zone).astimezone(UTC)


def _round_trip(local: datetime, zone: ZoneInfo) -> datetime:
    return local.replace(tzinfo=zone).astimezone(UTC).astimezone(zone).replace(tzinfo=None)
