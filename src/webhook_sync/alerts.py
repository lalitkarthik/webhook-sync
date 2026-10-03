"""Alerts to a Slack or Discord incoming webhook (spec "Alerts", A8, A17, Q139).

One JSON POST to `[alerts] url` carrying both `text` (Slack) and `content` (Discord), 10 s
timeout. Each kind is sent at most once an hour: a repeat within the hour is suppressed and
counted in the kind's `alerts` row, and the next message carries the count. A send that fails is
logged and not retried before the kind is next due. Bodies carry ids and counts only: emails,
keys and the spreadsheet id are masked before anything leaves. With no URL, a due alert is one
warning log line instead.

The dead-jobs check runs once a minute in the server: when the kind is due and jobs went dead
since its last alert, one `dead_jobs` alert gives their counts by stage.
"""

import asyncio
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Literal

import httpx
import sqlalchemy as sa
from sqlalchemy.dialects import sqlite

from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.db.schema import alerts, jobs
from webhook_sync.db.writer import Transaction, Writer
from webhook_sync.queue.pause import PauseTarget

log = logging.getLogger("webhook_sync.alerts")

type AlertKind = Literal[
    "paused",
    "dead_jobs",
    "sheet_changed",
    "unknown_currency",
    "silent_webhook",
    "verify_differences",
    "stale_twice",
]
"""P3's kinds, and P4's: events found only by the sweeper, verify's unrepaired differences,
and the same payment stale on two full runs in a row (Q155)."""

type Outcome = Literal["sent", "suppressed", "logged", "failed"]
"""What became of an alert: posted, held back by the hourly rule, logged for want of a URL, or
a POST that failed."""

EVERY = timedelta(hours=1)
"""Each kind is sent at most once in this long."""

TIMEOUT = 10.0
"""Seconds the webhook gets to answer."""

DEAD_JOBS_CHECK_EVERY = 60.0
"""Seconds between dead-jobs checks (A17)."""

SILENT_WEBHOOK_ADVICE = (
    "Check the webhook settings in your Razorpay dashboard. A webhook Razorpay disabled after"
    " 24 hours of failures stays disabled until you re-enable it under Accounts & Settings"
    " \u203a Webhooks."
)
"""What the `silent_webhook` alert tells the owner to do (sweeper.md, "Silent webhooks")."""

MASK = "***"
_EMAIL = re.compile(r"[^\s@<>()\[\]{}\"',;:]+@[^\s@<>()\[\]{}\"',;:]+")


class Alerter:
    """Sends alerts to one webhook URL (empty: a warning log line), at most hourly per kind."""

    def __init__(self, writer: Writer, url: str, *, redact: Iterable[str] = ()) -> None:
        self._writer = writer
        self._url = url
        self._redact = sorted({s for s in redact if s}, key=len, reverse=True)
        self._client = httpx.AsyncClient(timeout=TIMEOUT)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, kind: AlertKind, text: str) -> Outcome:
        """Send one alert of `kind` now, unless one was sent within the hour."""
        now = utc_now()
        async with self._writer.transaction() as tx:
            suppressed = await _take_slot(tx, kind, now)
        if suppressed is None:
            log.info("alert suppressed", extra={"alert": kind})
            return "suppressed"
        return await self._deliver(kind, text, suppressed)

    async def paused(self, target: PauseTarget, name: str, reason: str) -> None:
        """The `paused` alert: which source or sink, and why. Never raises."""
        await self._never_raise("paused", f"{target} {name} paused: {reason}")

    async def unknown_currency(self, held: Mapping[str, Sequence[str]]) -> None:
        """The `unknown_currency` alert: each unknown code with the payment ids held back.
        Never raises."""
        count = sum(len(ids) for ids in held.values())
        codes = "; ".join(f"{code}: {', '.join(ids)}" for code, ids in sorted(held.items()))
        await self._never_raise(
            "unknown_currency",
            f"{count} payment(s) held back from the sheet, their jobs dead ({codes})",
        )

    async def silent_webhook(self, found: str) -> None:
        """The `silent_webhook` alert: what the sweeper found that no delivery brought, and how
        to re-enable a webhook Razorpay disabled (Q149). Never raises."""
        await self._never_raise(
            "silent_webhook",
            f"Events are reaching us only through the sweeper: {found}. {SILENT_WEBHOOK_ADVICE}",
        )

    async def check_dead_jobs(self) -> Outcome | None:
        """One dead-jobs check: when `dead_jobs` is due and jobs went dead since its last alert,
        send their counts by stage. None when nothing is sent."""
        async with self._writer.transaction() as tx:
            now = utc_now()
            last_sent, _ = await _row(tx, "dead_jobs")
            if _within_the_hour(last_sent, now):
                return None
            died = sa.select(jobs.c.stage, sa.func.count().label("n")).where(
                jobs.c.status == "dead"
            )
            if last_sent is not None:
                died = died.where(jobs.c.updated_at > last_sent)
            counts = await tx.execute(died.group_by(jobs.c.stage).order_by(jobs.c.stage))
            if not counts:
                return None
            suppressed = await _take_slot(tx, "dead_jobs", now)
        assert suppressed is not None  # due, checked above in the same transaction
        total = sum(row["n"] for row in counts)
        by_stage = ", ".join(f"{row['stage']} {row['n']}" for row in counts)
        return await self._deliver(
            "dead_jobs", f"{total} new dead job{'s' * (total != 1)} ({by_stage})", suppressed
        )

    async def run_dead_jobs_check(self) -> None:
        """The server's loop: a dead-jobs check now, then once a minute, until cancelled."""
        while True:
            try:
                await self.check_dead_jobs()
            except Exception:
                log.exception("dead-jobs check failed")
            await asyncio.sleep(DEAD_JOBS_CHECK_EVERY)

    async def _never_raise(self, kind: AlertKind, text: str) -> None:
        try:
            await self.send(kind, text)
        except Exception:
            log.exception("alert not sent: error", extra={"alert": kind})

    async def _deliver(self, kind: AlertKind, text: str, suppressed: int) -> Outcome:
        message = self._mask(f"webhook-sync {kind}: {text}")
        if suppressed:
            message += f" ({suppressed} more suppressed since the last alert)"
        if not self._url:
            log.warning("alert not sent, no [alerts] url: %s", message, extra={"alert": kind})
            return "logged"
        try:
            answer = await self._client.post(self._url, json={"text": message, "content": message})
            answer.raise_for_status()
        except httpx.HTTPError as error:
            status = (
                error.response.status_code if isinstance(error, httpx.HTTPStatusError) else None
            )
            log.warning(
                "alert not sent: %s",
                type(error).__name__,
                extra={"alert": kind, "http_status": status},
            )
            return "failed"
        log.info("alert sent", extra={"alert": kind, "suppressed": suppressed})
        return "sent"

    def _mask(self, text: str) -> str:
        for secret in self._redact:
            text = text.replace(secret, MASK)
        return _EMAIL.sub(MASK, text)


async def _row(tx: Transaction, kind: AlertKind) -> tuple[str | None, int]:
    """The kind's `alerts` row: when it was last sent, and how many were suppressed since."""
    rows = await tx.execute(
        sa.select(alerts.c.last_sent_at, alerts.c.suppressed_count).where(alerts.c.kind == kind)
    )
    return (rows[0]["last_sent_at"], rows[0]["suppressed_count"]) if rows else (None, 0)


def _within_the_hour(last_sent: str | None, now: datetime) -> bool:
    return last_sent is not None and last_sent > to_db_text(now - EVERY)


async def _take_slot(tx: Transaction, kind: AlertKind, now: datetime) -> int | None:
    """If `kind` is due, mark it sent now and return how many were suppressed since its last
    send; otherwise count this one as suppressed and return None."""
    last_sent, suppressed = await _row(tx, kind)
    if _within_the_hour(last_sent, now):
        await tx.execute(
            sa.update(alerts)
            .where(alerts.c.kind == kind)
            .values(suppressed_count=alerts.c.suppressed_count + 1)
        )
        return None
    insert = sqlite.insert(alerts).values(
        kind=kind, last_sent_at=to_db_text(now), suppressed_count=0
    )
    await tx.execute(
        insert.on_conflict_do_update(
            index_elements=[alerts.c.kind],
            set_={"last_sent_at": insert.excluded.last_sent_at, "suppressed_count": 0},
        )
    )
    return suppressed
