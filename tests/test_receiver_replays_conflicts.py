"""Replays, conflicts and the account check (seam 1: the HTTP app in process)."""

import logging

import httpx
import pytest
from support import ReadRows, razorpay_sample, sign_razorpay

pytestmark = pytest.mark.unrecorded


def records(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == message]


async def test_the_same_body_under_a_new_event_id_is_a_duplicate_of_the_stored_event(
    client: httpx.AsyncClient, read_rows: ReadRows, caplog: pytest.LogCaptureFixture
) -> None:
    sample = razorpay_sample("payment.captured")
    body, headers = sign_razorpay(sample, event_id="evt_first01")
    replay, replay_headers = sign_razorpay(sample, event_id="evt_replayed01")
    assert replay == body
    await client.post("/webhooks/razorpay", content=body, headers=headers)

    with caplog.at_level(logging.INFO):
        response = await client.post("/webhooks/razorpay", content=replay, headers=replay_headers)

    assert response.status_code == 200
    assert response.json() == {"result": "duplicate"}
    assert await read_rows("SELECT event_id FROM events") == [{"event_id": "evt_first01"}]
    assert len(await read_rows("SELECT * FROM jobs")) == 1
    deliveries = await read_rows("SELECT event_id, result, status_code FROM deliveries ORDER BY id")
    assert [tuple(d.values()) for d in deliveries] == [
        ("evt_first01", "new", 202),
        ("evt_first01", "duplicate", 200),
    ]
    [stored] = records(caplog, "delivery stored")
    assert stored.__dict__["event_id"] == "evt_first01"
    assert stored.__dict__["header_event_id"] == "evt_replayed01"
