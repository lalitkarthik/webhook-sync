"""The admin listener in process (A2, Q133): `POST /admin/resume/{name}` and `GET /admin/status`,
on the running app's writer and pause cache.
"""

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from support import TEST_ACCOUNT, ReadRows, execute_aside
from time_machine import TimeMachineFixture

from webhook_sync.admin.app import create_admin_app
from webhook_sync.config import Config
from webhook_sync.db.migrations import apply_pending
from webhook_sync.db.writer import Writer
from webhook_sync.queue import Pauses
from webhook_sync.receiver.app import create_app

NINE = datetime(2026, 10, 2, 20, 30, tzinfo=UTC)
EARLIER = "2026-10-02T19:00:00.000000Z"


@pytest.fixture(autouse=True)
def frozen_clock(time_machine: TimeMachineFixture) -> None:
    time_machine.move_to(NINE, tick=False)


async def pause_razorpay_and_add_jobs(data_dir: Path) -> None:
    await execute_aside(
        data_dir,
        "INSERT INTO sinks (name, kind, state, reason, since) VALUES"
        f" ('razorpay', 'source', 'paused', 'Razorpay answered HTTP 401', '{EARLIER}')",
    )
    for i, (stage, status) in enumerate(
        [("ledger", "waiting"), ("ledger", "dead"), ("mirror", "waiting"), ("mirror", "waiting")]
    ):
        await execute_aside(
            data_dir,
            "INSERT INTO jobs (stage, sink, source, account, payment_key, status, rerun,"
            " attempts, max_attempts, run_after, created_at, updated_at) VALUES"
            f" ('{stage}', '{'ledger' if stage == 'ledger' else 'sheets'}', 'razorpay',"
            f" '{TEST_ACCOUNT}', 'pay_{i}', '{status}', 0, 0, 8, '{EARLIER}', '{EARLIER}',"
            f" '{EARLIER}')",
        )


@pytest.fixture
async def running(
    config: Config, data_dir: Path
) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    """A migrated database with a stored pause, the public app started, and its admin app."""
    writer = await Writer.open(data_dir)
    await apply_pending(writer)
    await writer.close()
    await pause_razorpay_and_add_jobs(data_dir)
    public = create_app(config, data_dir)
    async with public.router.lifespan_context(public):
        admin = create_admin_app(public, data_dir)
        transport = httpx.ASGITransport(app=admin)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
            yield public, c


async def test_status_lists_sinks_and_sources_jobs_by_stage_and_the_last_flush(
    running: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    _, admin = running

    answer = await admin.get("/admin/status")

    assert answer.status_code == 200
    assert answer.json() == {
        "sinks": [
            {
                "name": "razorpay",
                "kind": "source",
                "state": "paused",
                "reason": "Razorpay answered HTTP 401",
                "since": EARLIER,
            },
            {"name": "sheets", "kind": "sink", "state": "active", "reason": None, "since": None},
        ],
        "jobs": {"ledger": {"waiting": 1, "dead": 1}, "mirror": {"waiting": 2, "dead": 0}},
        "last_flush": {"at": None, "rows_written": 0},
    }


async def test_resume_clears_the_pause_in_sinks_and_the_running_cache_and_logs_who(
    running: tuple[FastAPI, httpx.AsyncClient],
    read_rows: ReadRows,
    caplog: pytest.LogCaptureFixture,
) -> None:
    public, admin = running
    pauses: Pauses = public.state.pauses
    assert pauses.sources == {"razorpay"}

    with caplog.at_level(logging.INFO, logger="webhook_sync.admin"):
        answer = await admin.post("/admin/resume/razorpay", headers={"x-wsync-by": "cli"})

    assert answer.status_code == 200
    assert answer.json() == {"name": "razorpay", "result": "resumed"}
    assert pauses.sources == set()
    assert await read_rows("SELECT state, reason, since FROM sinks WHERE name = 'razorpay'") == [
        {"state": "active", "reason": None, "since": "2026-10-02T20:30:00.000000Z"}
    ]
    [record] = [r for r in caplog.records if r.getMessage() == "pause cleared"]
    assert (record.__dict__["target"], record.__dict__["by"]) == ("razorpay", "cli")


async def test_resume_of_an_active_name_changes_nothing_and_an_unknown_one_is_404(
    running: tuple[FastAPI, httpx.AsyncClient],
) -> None:
    _, admin = running

    active = await admin.post("/admin/resume/sheets")
    unknown = await admin.post("/admin/resume/stripe")

    assert (active.status_code, active.json()["result"]) == (200, "active")
    assert (unknown.status_code, unknown.json()["result"]) == (404, "unknown")
