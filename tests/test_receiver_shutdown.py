"""Shutting down, as the HTTP app sees it (seam 1): 503 to new deliveries, counts written."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from support import TEST_ACCOUNT, ReadRows, razorpay_sample, sign_razorpay
from time_machine import TimeMachineFixture

from webhook_sync.clock import utc_now
from webhook_sync.config import Config
from webhook_sync.db.writer import Writer
from webhook_sync.queue import Pauses, claim, enqueue
from webhook_sync.receiver.app import create_app, shut_down

pytestmark = pytest.mark.unrecorded

NINE = datetime(2026, 10, 2, 9, 0, 10, tzinfo=UTC)


@asynccontextmanager
async def served(
    config: Config, data_dir: Path
) -> AsyncGenerator[tuple[FastAPI, httpx.AsyncClient]]:
    app = create_app(config, data_dir)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://wsync.test") as client:
            yield app, client


async def test_while_shutting_down_a_new_delivery_gets_503_and_nothing_is_stored(
    config: Config, data_dir: Path, read_rows: ReadRows
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_late01")

    async with served(config, data_dir) as (app, client):
        await shut_down(app)
        response = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 503
    assert response.json() == {"error": "shutting_down"}
    assert await read_rows("SELECT * FROM events") == []
    assert await read_rows("SELECT * FROM deliveries") == []
    assert await read_rows("SELECT * FROM jobs") == []


async def test_shutting_down_comes_before_every_other_check(
    config: Config, data_dir: Path, read_rows: ReadRows
) -> None:
    async with served(config, data_dir) as (app, client):
        await shut_down(app)
        response = await client.post(
            "/webhooks/razorpay", content=b"not json", headers={"content-type": "text/plain"}
        )

    assert response.status_code == 503
    assert await read_rows("SELECT * FROM rejection_counts") == []


async def test_shutting_down_writes_the_open_minutes_rejection_counts_before_serving_stops(
    config: Config, data_dir: Path, read_rows: ReadRows, time_machine: TimeMachineFixture
) -> None:
    time_machine.move_to(NINE, tick=False)
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), secret="not-the-secret")

    async with served(config, data_dir) as (app, client):
        await client.post("/webhooks/razorpay", content=body, headers=headers)
        await shut_down(app)
        written_before_the_app_stops = await read_rows(
            "SELECT reason, minute, count FROM rejection_counts"
        )

    assert written_before_the_app_stops == [
        {"reason": "bad_signature", "minute": "2026-10-02T09:00:00.000000Z", "count": 1}
    ]


async def leave_a_job_running(writer: Writer) -> None:
    async with writer.transaction() as tx:
        await enqueue(
            tx,
            stage="ledger",
            sink="ledger",
            source="razorpay",
            account=TEST_ACCOUNT,
            payment_key="pay_A",
            event_id="evt_A",
            retry_profile="production",
            now=utc_now(),
        )
    await claim(
        writer,
        stage="ledger",
        sink="ledger",
        limit=1,
        claimed_by="wsync-test@2026-10-02T09:00:00.000000Z",
        pauses=Pauses(),
        now=utc_now(),
    )


async def test_shutting_down_releases_a_job_left_running_with_its_attempt_refunded(
    config: Config, data_dir: Path, writer: Writer, read_rows: ReadRows
) -> None:
    await leave_a_job_running(writer)

    async with served(config, data_dir) as (app, _):
        await shut_down(app)
        after_shutdown = await read_rows("SELECT status, attempts FROM jobs")

    assert after_shutdown == [{"status": "waiting", "attempts": 0}]
    assert await read_rows("SELECT outcome FROM attempts") == [{"outcome": "released"}]


async def test_an_app_stopped_without_shut_down_still_releases_its_running_jobs(
    config: Config, data_dir: Path, writer: Writer, read_rows: ReadRows
) -> None:
    await leave_a_job_running(writer)

    async with served(config, data_dir):
        pass

    assert await read_rows("SELECT status, attempts FROM jobs") == [
        {"status": "waiting", "attempts": 0}
    ]
    assert await read_rows("SELECT outcome FROM attempts") == [{"outcome": "released"}]
