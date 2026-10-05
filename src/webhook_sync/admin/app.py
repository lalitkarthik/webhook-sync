"""The admin listener's app, bound to a loopback address only (A2, Q133): no tokens in the cut.

`POST /admin/resume/{name}` clears a pause in `sinks` and in the running server's claim-time
cache; `GET /admin/status` returns what `wsync status` prints. `POST /admin/sweep`,
`/admin/backfill` and `/admin/verify` start a run by hand on the server's sweeper and verify
and answer 202 with its id; `GET /admin/runs/{id}` reports it (Q152, A19). A sweep asked for
with `{"relist": true}` re-lists each list too, due or not. The app shares the public app's
writer, pause cache, sweeper and verify, set by the public app's lifespan; until that has run,
and to a run asked for once the shutdown has begun, it answers 503.
"""

import dataclasses
import logging
from collections.abc import Coroutine
from datetime import date
from pathlib import Path
from typing import Any, Self

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from webhook_sync.admin.runs import AdminRuns, Checks, Result, RunKind, backfill, sweep, verify
from webhook_sync.admin.status import read_status
from webhook_sync.clock import utc_now
from webhook_sync.queue import Pauses, resume

log = logging.getLogger("webhook_sync.admin")

BY_HEADER = "x-wsync-by"
"""Who asked, as the caller names itself (`wsync resume` sends `cli`); recorded in the log."""


class BackfillAsked(BaseModel):
    """`{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}`: local dates in `service.timezone`."""

    model_config = ConfigDict(populate_by_name=True)
    first: date = Field(alias="from")
    last: date = Field(alias="to")

    @model_validator(mode="after")
    def in_order(self) -> Self:
        if self.first > self.last:
            raise ValueError("from must not be after to")
        return self


class SweepAsked(BaseModel):
    """`relist` re-lists each list now, due or not: the payments of the last 7 days and the
    refunds of the last 21."""

    relist: bool = False


class VerifyAsked(BaseModel):
    """Quick by default; `full` checks Razorpay too, over `days` (default `[verify] days`), and
    repairs only when `repair` is set (a quick run always repairs)."""

    full: bool = False
    repair: bool = False
    days: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def days_only_when_full(self) -> Self:
        if self.days is not None and not self.full:
            raise ValueError("days applies to a full run only")
        return self


def create_admin_app(public: FastAPI, data_dir: Path) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    runs = AdminRuns()

    @app.post("/admin/resume/{name}")
    async def resume_pause(name: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        writer = getattr(public.state, "writer", None)
        pauses: Pauses | None = getattr(public.state, "pauses", None)
        if writer is None or pauses is None:
            return _starting()
        now = utc_now()
        result = await resume(writer, name, pauses=pauses, now=now)
        if result == "resumed":
            log.info(
                "pause cleared",
                extra={"target": name, "by": request.headers.get(BY_HEADER, "admin")},
            )
        return JSONResponse(
            {"name": name, "result": result}, status_code=404 if result == "unknown" else 200
        )

    @app.get("/admin/status")
    async def status() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if getattr(public.state, "writer", None) is None:
            return _starting()
        return JSONResponse(dict(await read_status(data_dir)))

    def checks() -> Checks | None:
        """The server's sweeper and verify; None while it starts or once its shutdown has
        begun, when a new run would escape the shutdown's cancelling."""
        if getattr(public.state, "writer", None) is None or public.state.receiver.shutting_down:
            return None
        return Checks(
            data_dir, public.state.sweeper, public.state.full_verify, public.state.sheet_verify
        )

    def start(kind: RunKind, work: Coroutine[Any, Any, Result], request: Request) -> JSONResponse:
        run = runs.start(kind, work, alongside=public.state.loops)
        log.info(
            "run by hand asked for",
            extra={"run_id": run.id, "kind": kind, "by": request.headers.get(BY_HEADER, "admin")},
        )
        return JSONResponse({"run_id": run.id, "kind": kind}, status_code=202)

    @app.post("/admin/sweep")
    async def sweep_now(request: Request, asked: SweepAsked | None = None) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if (on := checks()) is None:
            return _starting()
        return start("sweep", sweep(on, relist=asked is not None and asked.relist), request)

    @app.post("/admin/backfill")
    async def backfill_now(asked: BackfillAsked, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if (on := checks()) is None:
            return _starting()
        return start("backfill", backfill(on, asked.first, asked.last), request)

    @app.post("/admin/verify")
    async def verify_now(asked: VerifyAsked, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        if (on := checks()) is None:
            return _starting()
        work = verify(on, full=asked.full, repair=asked.repair, days=asked.days)
        return start("verify", work, request)

    @app.get("/admin/runs/{run_id}")
    async def run_report(run_id: str) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        run = runs.get(run_id)
        if run is None:
            return JSONResponse({"error": "no such run"}, status_code=404)
        return JSONResponse(dataclasses.asdict(run))

    return app


def _starting() -> JSONResponse:
    return JSONResponse({"error": "starting"}, status_code=503)
