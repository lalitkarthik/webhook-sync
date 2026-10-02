"""Each handled event is stored with its ledger job, the outbox (seam 1: the HTTP app)."""

import httpx
import pytest
from support import TEST_ACCOUNT, ReadRows, razorpay_sample, sign_razorpay

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
