"""What every verify run shares: its findings, work in progress, and recording the run.

A run is stored in `verify_runs` with its findings in `verify_findings` and, when it repairs, the
repair jobs, all in one transaction (verify.md, "Repair"): a mirror job rewrites a payment's
sheet rows, a ledger job fetches the payment again. Both are coalesced into a live job of the
same payment, so two findings of one payment share one job.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.config import Config
from webhook_sync.db.schema import jobs, verify_findings, verify_runs
from webhook_sync.db.writer import Writer
from webhook_sync.queue import enqueue
from webhook_sync.queue.operations import LIVE_STATUSES
from webhook_sync.sinks.encoding import zone_label
from webhook_sync.sinks.sheets import RAZORPAY
from webhook_sync.sinks.sheets import SINK as SHEETS_SINK
from webhook_sync.sinks.sheets import STAGE as MIRROR_STAGE

type FindingKind = Literal[
    "sheet_missing",
    "sheet_differs",
    "sheet_duplicate",
    "sheet_foreign",
    "summary_differs",
    "missing",
    "stale",
    "orphaned",
    "not_found",
    "unknown_columns",
]
"""The ledger against the sheet (`sheet_*`, `summary_differs`), then Razorpay against the
ledger; `not_found` and `unknown_columns` are rows the ledger worker flagged, listed only."""

type RunKind = Literal["quick", "full"]
"""Quick: the sheet against the ledger, hourly. Full: Razorpay too, nightly."""

type PaymentKey = tuple[str, str, str]

FEE_SETTLES = timedelta(hours=1)
"""A payment younger than this whose fee is pending is skipped: the fee is on its way (Q97)."""


@dataclass(frozen=True)
class Finding:
    kind: FindingKind
    payment_id: str | None
    detail: str | None = None
    repair: str | None = None
    """The payment whose job repairs it, when one can."""
    stage: Literal["mirror", "ledger"] = "mirror"
    """That job's stage: `mirror` rewrites the payment's rows, `ledger` fetches it again."""


def in_progress(row: Mapping[str, Any], started: datetime) -> bool:
    """Changed since the run started, or under an hour old with the fee pending."""
    if row["updated_at"] >= to_db_text(started):
        return True
    created = row["source_created_at"]
    young = created is not None and created > to_db_text(started - FEE_SETTLES)
    return young and row["fee_minor"] is None


async def live_payments(writer: Writer) -> set[PaymentKey]:
    """The keys of the payments with a live job of any stage."""
    async with writer.transaction() as tx:
        live = await tx.execute(
            sa.select(jobs.c.source, jobs.c.account, jobs.c.payment_key)
            .where(jobs.c.status.in_(LIVE_STATUSES))
            .distinct()
        )
    return {(r["source"], r["account"], r["payment_key"]) for r in live}


@dataclass(frozen=True)
class Recorded:
    run_id: int
    repaired: int
    """How many findings got a repair job."""


async def record_run(
    writer: Writer,
    config: Config,
    *,
    kind: RunKind,
    started: datetime,
    local_date: date,
    window_days: int,
    checked: int,
    findings: Sequence[Finding],
    line: str,
    repair: bool,
    ignored_events: int = 0,
    by_hand: bool = False,
) -> Recorded:
    """The run, its findings and, when `repair`, their repair jobs, in one transaction;
    `by_hand` marks a run asked for by a person, never a scheduled one."""
    account = config.razorpay.account
    now = utc_now()
    repaired = sum(1 for f in findings if repair and f.repair is not None)
    async with writer.transaction() as tx:
        [run] = await tx.execute(
            sa.insert(verify_runs)
            .values(
                kind=kind,
                started_at=to_db_text(started),
                finished_at=to_db_text(now),
                window_days=window_days,
                local_date=local_date.isoformat(),
                payments_checked=checked,
                findings_count=len(findings),
                repaired_count=repaired,
                ignored_events=ignored_events,
                outcome="differences" if findings else "clean",
                status_line=line,
                by_hand=by_hand,
            )
            .returning(verify_runs.c.id)
        )
        for finding in findings:
            job_id = None
            if repair and finding.repair is not None:
                ledger = finding.stage == "ledger"
                job_id = await enqueue(
                    tx,
                    stage="ledger" if ledger else MIRROR_STAGE,
                    sink="ledger" if ledger else SHEETS_SINK,
                    source=RAZORPAY,
                    account=account,
                    payment_key=finding.repair,
                    event_id=None,
                    retry_profile=config.service.retry_profile,
                    now=now,
                )
            await tx.execute(
                sa.insert(verify_findings).values(
                    run_id=run["id"],
                    kind=finding.kind,
                    source=RAZORPAY,
                    account=account,
                    payment_id=finding.payment_id,
                    detail=finding.detail,
                    repair_job_id=job_id,
                )
            )
    return Recorded(run["id"], repaired)


def local_time(moment: datetime, zone: ZoneInfo) -> str:
    """A run's time as its line shows it: `02 Oct 21:00 IST`."""
    return f"{moment.astimezone(zone):%d %b %H:%M} {zone_label(zone)}"


def counted(n: int, noun: str) -> str:
    return f"{n:,} {noun}" + ("" if n == 1 else "s")
