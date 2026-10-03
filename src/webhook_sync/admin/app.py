"""The admin listener's app, bound to a loopback address only (A2, Q133): no tokens in the cut.

`POST /admin/resume/{name}` clears a pause in `sinks` and in the running server's claim-time
cache; `GET /admin/status` returns what `wsync status` prints. It shares the public app's writer
and pause cache, set by the public app's lifespan; until that has run it answers 503.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from webhook_sync.admin.status import read_status
from webhook_sync.clock import utc_now
from webhook_sync.queue import Pauses, resume

log = logging.getLogger("webhook_sync.admin")

BY_HEADER = "x-wsync-by"
"""Who asked, as the caller names itself (`wsync resume` sends `cli`); recorded in the log."""


def create_admin_app(public: FastAPI, data_dir: Path) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.post("/admin/resume/{name}")
    async def resume_pause(name: str, request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        writer = getattr(public.state, "writer", None)
        pauses: Pauses | None = getattr(public.state, "pauses", None)
        if writer is None or pauses is None:
            return JSONResponse({"error": "starting"}, status_code=503)
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
            return JSONResponse({"error": "starting"}, status_code=503)
        return JSONResponse(dict(await read_status(data_dir)))

    return app
