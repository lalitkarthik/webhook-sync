"""Each handled event is stored with its ledger job, the outbox (seam 1: the HTTP app)."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from support import TEST_ACCOUNT, ReadRows, razorpay_sample, sign_razorpay
from time_machine import TimeMachineFixture

from webhook_sync.db.files import database_path

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


def force_job_state(data_dir: Path, job_id: int, *, status: str, prefetched: str | None) -> None:
    """Put a job in a state no P1 operation reaches through the app (claim comes with #12)."""
    with closing(sqlite3.connect(database_path(data_dir))) as connection, connection:
        connection.execute(
            "UPDATE jobs SET status = ?, prefetched = ? WHERE id = ?", (status, prefetched, job_id)
        )


async def test_a_second_event_for_a_payment_whose_job_runs_sets_rerun_on_that_job(
    client: httpx.AsyncClient, read_rows: ReadRows, data_dir: Path
) -> None:
    first, first_headers = sign_razorpay(payment_authorized(), event_id="evt_authorized01")
    await client.post("/webhooks/razorpay", content=first, headers=first_headers)
    [job_before] = await read_rows("SELECT * FROM jobs")
    force_job_state(data_dir, job_before["id"], status="running", prefetched='{"listed": true}')

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
