"""Sweeps, backfills and verifies asked for by hand (Q152, A19).

With a server holding the lock, the admin listener starts each as a run on the server's own
sweeper and verify and answers 202 with the run's id; `GET /admin/runs/{id}` reports it
running, then finished with its result, or failed with why. Runs are kept in memory while the
server lives (A19): what lasts is in `checkpoints`, `events` and `verify_runs`. With no server,
`wsync` does the same work in its own process under the lock, on the parts `on_their_own`
builds; any repair jobs then wait for the next `wsync serve`.

A sweep by hand is one ordinary sweep, without the heartbeat: a ping from a command would hide a
dead server from the heartbeat service. A full verify by hand repairs only when asked to.
"""

import asyncio
import logging
import secrets
from collections.abc import AsyncGenerator, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import httpx

from webhook_sync.admin.status import last_sweeps
from webhook_sync.alerts import Alerter
from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.config import Config
from webhook_sync.db.reader import Reader
from webhook_sync.db.writer import Writer
from webhook_sync.queue import Failure, load_pauses, register
from webhook_sync.receiver.app import (
    alert_redactions,
    configured_names,
    razorpay_adapter,
    sheets_client,
)
from webhook_sync.sinks.sheets import SINK as SHEETS_SINK
from webhook_sync.sources.razorpay import api_client
from webhook_sync.sweeper import Sweeper
from webhook_sync.verify import FullVerify, SheetVerify

log = logging.getLogger("webhook_sync.admin")

type RunKind = Literal["sweep", "backfill", "verify"]
type RunState = Literal["running", "finished", "failed"]
type Result = dict[str, Any]

NOTHING_TO_CHECK = (
    "nothing to check: the sheet is paused, has never been written, or could not be read;"
    " see the log"
)


class RunFailed(Exception):
    """The run could not do its work; the message says why in one line."""


@dataclass(frozen=True)
class Checks:
    """What a run by hand works on: the server's sweeper and verify, or ones of its own."""

    data_dir: Path
    sweeper: Sweeper
    full_verify: FullVerify
    sheet_verify: SheetVerify | None


async def sweep(checks: Checks) -> Result:
    """One sweep of both lists now; whether it caught up, and each list's last sweep."""
    finished = await checks.sweeper.sweep()
    async with Reader.open(checks.data_dir) as reader:
        return {"finished": finished, "sweeps": await last_sweeps(reader)}


async def backfill(checks: Checks, first: date, last: date) -> Result:
    """Payments created on the local dates `first` to `last` stored as `backfill:` events."""
    stored = await checks.sweeper.backfill(first, last)
    if isinstance(stored, Failure):
        raise RunFailed(f"the Razorpay list failed: {stored.error}")
    return {"from": first.isoformat(), "to": last.isoformat(), "stored": stored}


async def verify(checks: Checks, *, full: bool, repair: bool, days: int | None) -> Result:
    """One verify run now: quick (the sheet against the ledger), or full (Razorpay too, over
    `days` or `[verify] days`, repairing only when `repair`); the run and its findings."""
    if full:
        run_id = await checks.full_verify.run(repair=repair, days=days)
        if isinstance(run_id, Failure):
            raise RunFailed(f"a Razorpay list failed: {run_id.error}")
    else:
        run_id = await checks.sheet_verify.run() if checks.sheet_verify is not None else None
    if run_id is None:
        raise RunFailed(NOTHING_TO_CHECK)
    async with Reader.open(checks.data_dir) as reader:
        [run] = await reader.fetch_all(
            "SELECT kind, status_line, payments_checked, findings_count, repaired_count"
            " FROM verify_runs WHERE id = :run_id",
            {"run_id": run_id},
        )
        findings = await reader.fetch_all(
            "SELECT kind, payment_id, detail, repair_job_id FROM verify_findings"
            " WHERE run_id = :run_id ORDER BY id",
            {"run_id": run_id},
        )
    return {
        "verify_run_id": run_id,
        "kind": run["kind"],
        "line": run["status_line"],
        "payments_checked": run["payments_checked"],
        "findings_count": run["findings_count"],
        "repaired_count": run["repaired_count"],
        "findings": [
            {
                "kind": f["kind"],
                "payment_id": f["payment_id"],
                "detail": f["detail"],
                "repaired": f["repair_job_id"] is not None,
            }
            for f in findings
        ],
    }


@dataclass
class Run:
    id: str
    kind: RunKind
    started_at: str
    state: RunState = "running"
    finished_at: str | None = None
    result: Result | None = None
    error: str | None = None


class AdminRuns:
    """The server's runs by hand, by id, while it lives (A19)."""

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}

    def start(
        self,
        kind: RunKind,
        work: Coroutine[Any, Any, Result],
        *,
        alongside: list[asyncio.Task[None]],
    ) -> Run:
        """Start `work` as a run; its task joins `alongside` (the server's loops, which the
        shutdown cancels) until it ends."""
        run = Run(secrets.token_hex(8), kind, to_db_text(utc_now()))
        self._runs[run.id] = run
        task = asyncio.create_task(self._carry(run, work))
        alongside.append(task)
        task.add_done_callback(alongside.remove)
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    async def _carry(self, run: Run, work: Coroutine[Any, Any, Result]) -> None:
        log.info("run by hand started", extra={"run_id": run.id, "kind": run.kind})
        try:
            run.result = await work
            run.state = "finished"
        except RunFailed as failed:
            run.state, run.error = "failed", str(failed)
        except Exception as error:
            log.exception("run by hand raised", extra={"run_id": run.id, "kind": run.kind})
            run.state, run.error = "failed", f"{type(error).__name__}: see the server's log"
        run.finished_at = to_db_text(utc_now())
        log.info(
            "run by hand ended", extra={"run_id": run.id, "kind": run.kind, "state": run.state}
        )


@asynccontextmanager
async def on_their_own(config: Config, writer: Writer) -> AsyncGenerator[Checks]:
    """A sweeper and verify of their own, for a command run with no server: built as the
    server's lifespan builds them, on the pauses `sinks` holds. Nothing flushes here, so a
    run's line and repairs reach the sheet at the next `wsync serve`."""
    razorpay_api = api_client(
        config.razorpay.api_url, config.razorpay_secrets.key_id, config.razorpay_secrets.key_secret
    )
    sheets_http = httpx.AsyncClient()
    alerter = Alerter(writer, config.alerts.url, redact=alert_redactions(config))
    try:
        await register(writer, configured_names(config))
        pauses = await load_pauses(writer)
        pauses.on_pause = alerter.paused
        razorpay = razorpay_adapter(config, razorpay_api)
        sheet_verify = (
            SheetVerify(
                writer,
                sheets_client(config, sheets_http),
                config,
                pauses=pauses,
                request_flush=lambda: None,
            )
            if SHEETS_SINK in config.sinks
            else None
        )
        yield Checks(
            config.service.data_dir,
            Sweeper(writer, razorpay, config, pauses=pauses, alerter=alerter),
            FullVerify(
                writer, razorpay, config, pauses=pauses, alerter=alerter, sheet=sheet_verify
            ),
            sheet_verify,
        )
    finally:
        await razorpay_api.aclose()
        await sheets_http.aclose()
        await alerter.aclose()
