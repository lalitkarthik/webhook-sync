"""The public app: `POST /webhooks/razorpay` and `GET /health` (receiver.md).

Built from a config object and a data directory. Its lifespan opens the writer and applies
pending migrations before the first request, starts the stage runners and the reaper, and closes
the writer at the end. `shut_down` runs the shutdown's steps while the app still answers.
"""

import asyncio
import logging
import os
import socket
import sqlite3
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.config import Config
from webhook_sync.db.migrations import apply_pending
from webhook_sync.db.writer import Writer
from webhook_sync.ledger import ledger_runner
from webhook_sync.queue import Pauses, Runners, run_reaper
from webhook_sync.receiver.checks import check_content_type, check_security_headers, read_body
from webhook_sync.receiver.demo import FailFirst
from webhook_sync.receiver.rate_cap import RateCap
from webhook_sync.receiver.rejections import RejectionCounters
from webhook_sync.receiver.store import store_delivery
from webhook_sync.sources.base import EventDescription, Rejection, SourceAdapter, VerifiedDelivery
from webhook_sync.sources.razorpay import EVENT_ID_HEADER, RazorpayAdapter, api_client

log = logging.getLogger("webhook_sync.receiver")


@dataclass
class Receiver:
    """What the routes share: the config, the writer, the source adapters, the rate cap and the
    rejection counters."""

    config: Config
    writer: Writer
    razorpay: SourceAdapter
    rate_cap: RateCap
    rejections: RejectionCounters
    fail_first: FailFirst | None = None
    """The demo fail-first switch, when `wsync serve --demo-fail-first N` turned it on."""
    shutting_down: bool = False
    """Set by `shut_down`: every new delivery is answered 503 (check 1)."""


def create_app(config: Config, data_dir: Path, demo_fail_first: int | None = None) -> FastAPI:
    """The public app; `demo_fail_first` is N of `--demo-fail-first N`, checked at start."""
    data_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        writer = await Writer.open(data_dir)
        razorpay_api = api_client(
            config.razorpay.api_url,
            config.razorpay_secrets.key_id,
            config.razorpay_secrets.key_secret,
        )
        try:
            # Under `wsync serve`, start() has already migrated under the lock and this is a
            # no-op; it migrates an app built in process (tests).
            applied = await apply_pending(writer)
            if applied:
                log.info("migrations applied: %s", ", ".join(f"{v:04d}" for v in applied))
            receiver = Receiver(
                config=config,
                writer=writer,
                razorpay=RazorpayAdapter(
                    mode=config.razorpay.mode,
                    account=config.razorpay.account,
                    webhook_secrets=config.razorpay_secrets.webhook_secrets,
                    api=razorpay_api,
                ),
                rate_cap=RateCap(),
                rejections=RejectionCounters(),
                fail_first=FailFirst(demo_fail_first) if demo_fail_first is not None else None,
            )
            app.state.receiver = receiver
            # One instance id and one pause set, shared by every stage runner (A15).
            claimed_by = instance_id()
            pauses = Pauses()
            ledger = ledger_runner(
                writer,
                {receiver.razorpay.name: receiver.razorpay},
                mirror_sinks=config.sinks,
                claimed_by=claimed_by,
                pauses=pauses,
                retry_profile=config.service.retry_profile,
            )
            runners = Runners(writer, [ledger])
            app.state.runners = runners
            runners.start()
            minutely = asyncio.create_task(_write_rejection_counts_every_minute(receiver))
            reaper = asyncio.create_task(
                run_reaper(writer, retry_profile=config.service.retry_profile)
            )
            try:
                yield
            finally:
                if not receiver.shutting_down:  # stopped some other way than `shut_down`
                    await shut_down(app)
                reaper.cancel()
                with suppress(asyncio.CancelledError):
                    await reaper
                minutely.cancel()
                with suppress(asyncio.CancelledError):
                    await minutely
                await write_all_rejection_counts(receiver)
        finally:
            await razorpay_api.aclose()
            await writer.close()

    app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/health")
    async def health() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    @app.post("/webhooks/razorpay")
    async def razorpay_webhook(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        started = time.perf_counter()
        received_at = utc_now()
        receiver: Receiver = request.app.state.receiver
        adapter = receiver.razorpay
        if receiver.shutting_down:
            # Check 1: a delivery taken now might not commit; 503 makes the source retry (Q37).
            return JSONResponse({"error": "shutting_down"}, status_code=503)
        if receiver.rejections.has_closed_minutes():
            await _write_rejection_counts(receiver)
        checked = await _check(request, receiver.rate_cap, adapter)
        if isinstance(checked, Rejection):
            receiver.rejections.count(adapter.name, checked.reason)
            return _rejected(adapter.name, checked, request.headers.get(EVENT_ID_HEADER))
        verified, description = checked
        if description.account_id is None:
            log.warning(
                "delivery names no account: stored under the configured account",
                extra={"source": adapter.name, "event_id": verified.event_id},
            )

        try:
            stored = await store_delivery(
                receiver.writer,
                source=adapter.name,
                account=receiver.config.razorpay.account,
                mode=receiver.config.razorpay.mode,
                retry_profile=receiver.config.service.retry_profile,
                delivery=verified,
                description=description,
                received_at=received_at,
                started=started,
                fail_first=receiver.fail_first,
            )
        except sqlite3.Error:
            # The transaction rolled back as a whole: nothing is stored, so the source retries.
            log.exception(
                "delivery not stored: database error",
                extra={
                    "source": adapter.name,
                    "event_id": verified.event_id,
                    "payment_key": description.payment_key,
                    "status_code": 503,
                },
            )
            return JSONResponse({"error": "database_error"}, status_code=503)
        if stored.result == "conflict":
            # Until alerts exist, a conflict is logged at error level and counted (Q116).
            log.error(
                "delivery conflicts with the stored event: first event kept",
                extra={
                    "source": adapter.name,
                    "event_id": stored.event_id,
                    "payment_key": description.payment_key,
                    "fingerprint": description.fingerprint,
                    "stored_fingerprint": stored.stored_fingerprint,
                },
            )
        stored_fields = {
            "source": adapter.name,
            "event_id": stored.event_id,
            "payment_key": description.payment_key,
            "job_id": stored.job_id,
            "result": stored.result,
            "status_code": stored.status_code,
            "reason": description.ignore_reason,
            # A body-hash replay is stored under the first event's id; the header's id (A17).
            "header_event_id": (
                verified.event_id if verified.event_id != stored.event_id else None
            ),
        }
        if stored.attempt is not None:
            # The demo switch logs each attempt for the recording spike to time (G38, G48).
            stored_fields |= {
                "attempt": stored.attempt,
                "received_at": to_db_text(received_at),
                "body_sha256": verified.body_sha256,
            }
        log.info("delivery stored", extra=stored_fields)
        return JSONResponse({"result": stored.result}, status_code=stored.status_code)

    return app


def instance_id() -> str:
    """This server's id on the jobs it claims (`claimed_by`): host, process id and start time."""
    return f"{socket.gethostname()}:{os.getpid()}@{to_db_text(utc_now())}"


async def shut_down(app: FastAPI) -> None:
    """The shutdown's steps before the listener closes (operations.md): answer 503 to new
    deliveries → stop claiming → up to 20 s for running jobs → release unfinished leases →
    write the rejection counts, the open minute's included. The caller then stops listening
    and releases the lock."""
    receiver: Receiver = app.state.receiver
    receiver.shutting_down = True
    runners: Runners = app.state.runners
    await runners.stop()
    await write_all_rejection_counts(receiver)


async def _write_rejection_counts(receiver: Receiver) -> None:
    """Write the closed minutes' rejection counts; a failure keeps them for the next try."""
    try:
        await receiver.rejections.write_closed_minutes(receiver.writer)
    except sqlite3.Error:
        log.exception("rejection counts not written: database error")


async def _write_rejection_counts_every_minute(receiver: Receiver) -> None:
    """While no request arrives to write them, closed minutes are written about once a minute."""
    while True:
        await asyncio.sleep(61 - utc_now().second)  # just past the next minute
        await _write_rejection_counts(receiver)


async def write_all_rejection_counts(receiver: Receiver) -> None:
    """The shutdown's last write: every count, the open minute's included."""
    try:
        await receiver.rejections.write_all(receiver.writer)
    except sqlite3.Error:
        log.exception("rejection counts lost at shutdown: database error")


async def _check(
    request: Request, rate_cap: RateCap, adapter: SourceAdapter
) -> tuple[VerifiedDelivery, EventDescription] | Rejection:
    """Checks 2-7 in receiver.md's order: the first that fails is the answer."""
    if not rate_cap.allow(adapter.name):
        return Rejection("rate_capped", 429)
    raw_body = await read_body(request)
    if isinstance(raw_body, Rejection):
        return raw_body
    if rejection := check_content_type(request.headers.get("content-type")):
        return rejection
    if rejection := check_security_headers(request.headers):
        return rejection
    verified = adapter.verify(raw_body, request.headers)
    if isinstance(verified, Rejection):
        return verified
    description = adapter.describe(verified)
    if isinstance(description, Rejection):
        return description
    return verified, description


def _rejected(source: str, rejection: Rejection, header_event_id: str | None) -> JSONResponse:
    log.warning(
        "delivery rejected",
        extra={
            "source": source,
            "reason": rejection.reason,
            "status_code": rejection.status_code,
            "header_event_id": header_event_id,
        },
    )
    return JSONResponse({"rejection": rejection.reason}, status_code=rejection.status_code)
