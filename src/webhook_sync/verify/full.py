"""The full run (verify.md, Q151, Q154): Razorpay against the ledger, then the sheet against it.

The app's lifespan runs it nightly: once per local date in `service.timezone`, at the first valid
`[verify] nightly_at` at or after that time (a daylight-saving change can skip it), and at start
when today's time has passed with no full run stored for today, so a server down at 02:00 runs
it on start the same day. "Once per local date" is read from `verify_runs`, so it survives
restarts (A18). While Razorpay is paused the source half is skipped: the sheet is checked as a
quick run.

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
from webhook_sync.db.schema import verify_findings, verify_runs
from webhook_sync.db.writer import Writer
from webhook_sync.queue import Failure, Pauses
from webhook_sync.sources.razorpay import RazorpayAdapter
from webhook_sync.verify.runs import Finding, counted, local_time, record_run
from webhook_sync.verify.sheet import SheetVerify
from webhook_sync.verify.source import RazorpayVerify

log = logging.getLogger("webhook_sync.verify")

type Sleep = Callable[[float], Awaitable[None]]


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
        cancelled. A run that fails or raises is not tried again that date."""
        tried: date | None = None
        while True:
            now = utc_now()
            night = now.astimezone(self._zone).date()
            if night == tried or await self._ran_on(night):
                night += timedelta(days=1)
            due = nightly_due(night, self._config.verify.nightly_at, self._zone)
            if due > now:
                log.info(
                    "nightly verify waiting",
                    extra={"local_date": night.isoformat(), "due_at": to_db_text(due)},
                )
                await self._sleep((due - now).total_seconds())
            try:
                await self.run(local_date=night)
            except Exception:
                log.exception("nightly verify failed; the next one runs tomorrow")
            tried = night

    async def run(
        self, *, repair: bool | None = None, local_date: date | None = None
    ) -> int | None:
        """One full run; its `verify_runs` id, or None when a list failed. `repair` defaults
        to `[verify] auto_repair`; `local_date` (the night it is for) to today's."""
        started = utc_now()
        clock = time.perf_counter()
        if self._razorpay.name in self._pauses.sources:
            log.info("full verify: Razorpay is paused, only the sheet is checked")
            return await self._sheet.run() if self._sheet is not None else None
        source = await self._source.check(started)
        if isinstance(source, Failure):
            log.warning(
                "full verify stopped: a list failed",
                extra={"error": source.error, "error_class": source.error_class},
            )
            return None
        sheet = await self._sheet.check(started) if self._sheet is not None else None
        findings = source.findings + (sheet.findings if sheet is not None else [])
        days = self._config.verify.days
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
                .where(verify_runs.c.kind == "full", verify_runs.c.local_date == night.isoformat())
                .limit(1)
            )
        return bool(rows)

    async def _stale_on_the_run_before(self, run_id: int) -> set[str]:
        """The payments found stale by the full run before `run_id`."""
        before = (
            sa.select(sa.func.max(verify_runs.c.id))
            .where(verify_runs.c.kind == "full", verify_runs.c.id < run_id)
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
