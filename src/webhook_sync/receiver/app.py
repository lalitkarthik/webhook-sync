"""The public app: `POST /webhooks/razorpay` and `GET /health` (receiver.md).

Built from a config object and a data directory. Its lifespan opens the writer and applies
pending migrations before the first request, and closes the writer at the end.
"""

import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from webhook_sync.clock import utc_now
from webhook_sync.config import Config
from webhook_sync.db.migrations import apply_pending
from webhook_sync.db.writer import Writer
from webhook_sync.receiver.store import store_delivery
from webhook_sync.sources.base import Rejection, SourceAdapter
from webhook_sync.sources.razorpay import EVENT_ID_HEADER, RazorpayAdapter

log = logging.getLogger("webhook_sync.receiver")


@dataclass
class Receiver:
    """What the routes share: the config, the writer and the source adapters."""

    config: Config
    writer: Writer
    razorpay: SourceAdapter


def create_app(config: Config, data_dir: Path) -> FastAPI:
    data_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        writer = await Writer.open(data_dir)
        try:
            applied = await apply_pending(writer)
            if applied:
                log.info("migrations applied: %s", ", ".join(f"{v:04d}" for v in applied))
            app.state.receiver = Receiver(
                config=config,
                writer=writer,
                razorpay=RazorpayAdapter(
                    mode=config.razorpay.mode,
                    webhook_secrets=config.razorpay_secrets.webhook_secrets,
                ),
            )
            yield
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
        raw_body = await request.body()

        verified = adapter.verify(raw_body, request.headers)
        if isinstance(verified, Rejection):
            return _rejected(adapter.name, verified, request.headers.get(EVENT_ID_HEADER))
        description = adapter.describe(verified)
        if isinstance(description, Rejection):
            return _rejected(adapter.name, description, verified.event_id)

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
        log.info(
            "delivery stored",
            extra={
                "source": adapter.name,
                "event_id": stored.event_id,
                "payment_key": description.payment_key,
                "result": stored.result,
                "status_code": stored.status_code,
                "reason": description.ignore_reason,
            },
        )
        return JSONResponse({"result": stored.result}, status_code=stored.status_code)

    return app


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
