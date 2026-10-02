"""The public app: `POST /webhooks/razorpay` and `GET /health` (receiver.md).

Built from a config object and a data directory. Its lifespan opens the writer and applies
pending migrations before the first request, and closes the writer at the end.
"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from webhook_sync.clock import utc_now
from webhook_sync.config import Config
from webhook_sync.db.migrations import apply_pending
from webhook_sync.db.writer import Writer
from webhook_sync.receiver.checks import check_content_type, check_security_headers, read_body
from webhook_sync.receiver.rate_cap import RateCap
from webhook_sync.receiver.rejections import RejectionCounters
from webhook_sync.receiver.store import store_delivery
from webhook_sync.sources.base import EventDescription, Rejection, SourceAdapter, VerifiedDelivery
from webhook_sync.sources.razorpay import EVENT_ID_HEADER, RazorpayAdapter

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


def create_app(config: Config, data_dir: Path) -> FastAPI:
    data_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        writer = await Writer.open(data_dir)
        try:
            applied = await apply_pending(writer)
            if applied:
                log.info("migrations applied: %s", ", ".join(f"{v:04d}" for v in applied))
            receiver = Receiver(
                config=config,
                writer=writer,
                razorpay=RazorpayAdapter(
                    mode=config.razorpay.mode,
                    webhook_secrets=config.razorpay_secrets.webhook_secrets,
                ),
                rate_cap=RateCap(),
                rejections=RejectionCounters(),
            )
            app.state.receiver = receiver
            minutely = asyncio.create_task(_write_rejection_counts_every_minute(receiver))
            try:
                yield
            finally:
                minutely.cancel()
                with suppress(asyncio.CancelledError):
                    await minutely
                await write_all_rejection_counts(receiver)
        finally:
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
        if receiver.rejections.has_closed_minutes():
            await _write_rejection_counts(receiver)
        checked = await _check(request, receiver.rate_cap, adapter)
        if isinstance(checked, Rejection):
            receiver.rejections.count(adapter.name, checked.reason)
            return _rejected(adapter.name, checked, request.headers.get(EVENT_ID_HEADER))
        verified, description = checked

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
        log.info(
            "delivery stored",
            extra={
                "source": adapter.name,
                "event_id": stored.event_id,
                "payment_key": description.payment_key,
                "result": stored.result,
                "status_code": stored.status_code,
                "reason": description.ignore_reason,
                # A body-hash replay is stored under the first event's id; the header's id (A17).
                "header_event_id": (
                    verified.event_id if verified.event_id != stored.event_id else None
                ),
            },
        )
        return JSONResponse({"result": stored.result}, status_code=stored.status_code)

    return app


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
