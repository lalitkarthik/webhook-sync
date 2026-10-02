"""Each handled event is stored with its ledger job, the outbox (seam 1: the HTTP app)."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from support import TEST_ACCOUNT, ReadRows, execute_aside, razorpay_sample, sign_razorpay
from time_machine import TimeMachineFixture

from webhook_sync.clock import utc_now
from webhook_sync.config import Config
from webhook_sync.db.files import database_path
from webhook_sync.db.writer import Writer
from webhook_sync.queue import Pauses, claim, complete
from webhook_sync.receiver.app import create_app

pytestmark = pytest.mark.unrecorded


async def test_a_handled_event_is_stored_with_one_waiting_ledger_job(
    client: httpx.AsyncClient, read_rows: ReadRows
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_captured01")

    response = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 202
    jobs = await read_rows("SELECT * FROM jobs")
    assert len(jobs) == 1
    job = jobs[0]
    assert (job["stage"], job["sink"], job["source"], job["account"], job["payment_key"]) == (
        "ledger",
        "ledger",
        "razorpay",
        TEST_ACCOUNT,
        "pay_DESlfW9H8K9uqM",
    )
    assert (job["status"], job["rerun"], job["attempts"]) == ("waiting", 0, 0)
    assert job["max_attempts"] == 10
    assert job["caused_by_event_id"] == "evt_captured01"
    assert job["prefetched"] is None
    assert job["run_after"] == job["created_at"]
    [event] = await read_rows("SELECT outcome, job_id FROM events")
    assert event == {"outcome": "queued", "job_id": job["id"]}


async def test_a_job_in_the_demo_retry_profile_is_allowed_six_attempts(
    config: Config, data_dir: Path, read_rows: ReadRows
) -> None:
    demo = replace(config, service=replace(config.service, retry_profile="demo"))
    app = create_app(demo, data_dir)
    body, headers = sign_razorpay(razorpay_sample("payment.captured"))

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client,
    ):
        await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert await read_rows("SELECT max_attempts FROM jobs") == [{"max_attempts": 6}]


def payment_authorized() -> dict[str, Any]:
    """The same payment as the payment.captured sample, one step earlier: authorized."""
    body = razorpay_sample("payment.captured")
    body["event"] = "payment.authorized"
    body["created_at"] -= 5
    body["payload"]["payment"]["entity"]["status"] = "authorized"
    body["payload"]["payment"]["entity"]["captured"] = False
    return body


async def test_a_second_event_for_a_payment_whose_job_waits_folds_into_that_job(
    client: httpx.AsyncClient, read_rows: ReadRows, time_machine: TimeMachineFixture
) -> None:
    time_machine.move_to(datetime(2026, 10, 2, 9, 0, tzinfo=UTC), tick=False)
    first, first_headers = sign_razorpay(payment_authorized(), event_id="evt_authorized01")
    await client.post("/webhooks/razorpay", content=first, headers=first_headers)
    [job_before] = await read_rows("SELECT * FROM jobs")

    time_machine.move_to(datetime(2026, 10, 2, 9, 0, 3, tzinfo=UTC), tick=False)
    second, second_headers = sign_razorpay(
        razorpay_sample("payment.captured"), event_id="evt_captured01"
    )
    response = await client.post("/webhooks/razorpay", content=second, headers=second_headers)

    assert response.status_code == 202
    [job] = await read_rows("SELECT * FROM jobs")
    assert job["id"] == job_before["id"]
    assert (job["status"], job["rerun"]) == ("waiting", 0)
    assert job["run_after"] == job_before["run_after"] == "2026-10-02T09:00:00.000000Z"
    assert job["caused_by_event_id"] == "evt_captured01"
    assert job["updated_at"] == "2026-10-02T09:00:03.000000Z"
    events = await read_rows("SELECT event_id, job_id FROM events ORDER BY event_id")
    assert events == [
        {"event_id": "evt_authorized01", "job_id": job["id"]},
        {"event_id": "evt_captured01", "job_id": job["id"]},
    ]


async def claim_ledger_job(data_dir: Path, *, complete_it: bool = False) -> None:
    """Claim the one waiting ledger job through the queue, as P2's ledger worker will."""
    writer = await Writer.open(data_dir)
    try:
        [job] = await claim(
            writer,
            stage="ledger",
            sink="ledger",
            limit=1,
            claimed_by="test-worker",
            pauses=Pauses(),
            now=utc_now(),
        )
        if complete_it:
            async with writer.transaction() as tx:
                await complete(tx, job, now=utc_now())
    finally:
        await writer.close()


def set_prefetched(data_dir: Path, job_id: int, prefetched: str) -> None:
    """Give a job a listed state; no P1 operation sets one (P4's backfill does)."""
    with closing(sqlite3.connect(database_path(data_dir))) as connection, connection:
        connection.execute("UPDATE jobs SET prefetched = ? WHERE id = ?", (prefetched, job_id))


async def test_a_second_event_for_a_payment_whose_job_runs_sets_rerun_on_that_job(
    client: httpx.AsyncClient, read_rows: ReadRows, data_dir: Path
) -> None:
    first, first_headers = sign_razorpay(payment_authorized(), event_id="evt_authorized01")
    await client.post("/webhooks/razorpay", content=first, headers=first_headers)
    await claim_ledger_job(data_dir)
    [job_before] = await read_rows("SELECT * FROM jobs")
    set_prefetched(data_dir, job_before["id"], '{"listed": true}')

    second, second_headers = sign_razorpay(
        razorpay_sample("payment.captured"), event_id="evt_captured01"
    )
    response = await client.post("/webhooks/razorpay", content=second, headers=second_headers)

    assert response.status_code == 202
    [job] = await read_rows("SELECT * FROM jobs")
    assert job["id"] == job_before["id"]
    assert (job["status"], job["rerun"], job["prefetched"]) == ("running", 1, None)
    assert job["run_after"] == job_before["run_after"]
    assert job["caused_by_event_id"] == "evt_captured01"
    [event] = await read_rows("SELECT job_id FROM events WHERE event_id = 'evt_captured01'")
    assert event["job_id"] == job["id"]


async def test_new_work_for_a_payment_whose_job_is_done_makes_a_new_job(
    client: httpx.AsyncClient, read_rows: ReadRows, data_dir: Path
) -> None:
    first, first_headers = sign_razorpay(payment_authorized(), event_id="evt_authorized01")
    await client.post("/webhooks/razorpay", content=first, headers=first_headers)
    await claim_ledger_job(data_dir, complete_it=True)

    second, second_headers = sign_razorpay(
        razorpay_sample("payment.captured"), event_id="evt_captured01"
    )
    await client.post("/webhooks/razorpay", content=second, headers=second_headers)

    jobs = await read_rows("SELECT id, status, caused_by_event_id FROM jobs ORDER BY id")
    assert [(j["status"], j["caused_by_event_id"]) for j in jobs] == [
        ("done", "evt_authorized01"),
        ("waiting", "evt_captured01"),
    ]
    [event] = await read_rows("SELECT job_id FROM events WHERE event_id = 'evt_captured01'")
    assert event["job_id"] == jobs[1]["id"]


@pytest.mark.parametrize("event_type", ["order.paid", "payment.dispute.created"])
async def test_an_ignored_event_makes_no_job(
    client: httpx.AsyncClient, read_rows: ReadRows, event_type: str
) -> None:
    body, headers = sign_razorpay(razorpay_sample(event_type), event_id="evt_ignored01")

    response = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 202
    assert await read_rows("SELECT * FROM jobs") == []
    assert await read_rows("SELECT job_id FROM events") == [{"job_id": None}]


async def test_a_handled_event_that_names_no_payment_is_stored_as_ignored_with_no_job(
    client: httpx.AsyncClient, read_rows: ReadRows
) -> None:
    body, headers = sign_razorpay(
        {
            "entity": "event",
            "account_id": TEST_ACCOUNT,
            "event": "refund.created",
            "payload": {"refund": {"entity": {"id": "rfnd_FP8QHiV938haTz", "amount": 500}}},
            "created_at": 1600000000,
        }
    )

    response = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 202
    events = await read_rows("SELECT type, outcome, ignore_reason, payment_key, job_id FROM events")
    assert events == [
        {
            "type": "refund.created",
            "outcome": "ignored",
            "ignore_reason": "no_payment",
            "payment_key": None,
            "job_id": None,
        }
    ]
    assert await read_rows("SELECT * FROM jobs") == []


async def test_a_duplicate_makes_no_second_job_and_leaves_the_first_untouched(
    client: httpx.AsyncClient, read_rows: ReadRows, data_dir: Path
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_captured01")
    await client.post("/webhooks/razorpay", content=body, headers=headers)
    await claim_ledger_job(data_dir)
    [job_before] = await read_rows("SELECT * FROM jobs")
    replayed_headers = {**headers, "x-razorpay-event-id": "evt_replayed01"}

    again = await client.post("/webhooks/razorpay", content=body, headers=headers)
    replayed = await client.post("/webhooks/razorpay", content=body, headers=replayed_headers)

    assert (again.status_code, replayed.status_code) == (200, 200)
    [job] = await read_rows("SELECT * FROM jobs")
    assert job == job_before
    assert job["status"] == "running"


async def test_a_database_error_in_the_transaction_is_503_and_stores_nothing(
    client: httpx.AsyncClient, read_rows: ReadRows, data_dir: Path
) -> None:
    # The delivery row is the transaction's last write: failing it comes after the event and
    # its job are written, so all three must roll back together.
    await execute_aside(
        data_dir,
        "CREATE TRIGGER fail_deliveries BEFORE INSERT ON deliveries "
        "BEGIN SELECT RAISE(ABORT, 'forced failure'); END",
    )
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_captured01")

    response = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 503
    assert await read_rows("SELECT * FROM events") == []
    assert await read_rows("SELECT * FROM jobs") == []
    assert await read_rows("SELECT * FROM deliveries") == []

    await execute_aside(data_dir, "DROP TRIGGER fail_deliveries")
    retried = await client.post("/webhooks/razorpay", content=body, headers=headers)
    assert retried.status_code == 202
    assert len(await read_rows("SELECT * FROM jobs")) == 1
