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


async def test_a_known_event_id_with_a_different_fingerprint_is_a_conflict_and_keeps_the_first(
    client: httpx.AsyncClient, read_rows: ReadRows, caplog: pytest.LogCaptureFixture
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_known01")
    await client.post("/webhooks/razorpay", content=body, headers=headers)
    [stored_before] = await read_rows("SELECT * FROM events")
    different = razorpay_sample("payment.captured")
    different["created_at"] += 60  # a different event: its fingerprint differs
    other, other_headers = sign_razorpay(different, event_id="evt_known01")

    with caplog.at_level(logging.INFO):
        response = await client.post("/webhooks/razorpay", content=other, headers=other_headers)

    assert response.status_code == 200
    assert response.json() == {"result": "conflict"}
    assert await read_rows("SELECT * FROM events") == [stored_before]
    assert len(await read_rows("SELECT * FROM jobs")) == 1
    deliveries = await read_rows("SELECT event_id, result, status_code FROM deliveries ORDER BY id")
    assert [tuple(d.values()) for d in deliveries] == [
        ("evt_known01", "new", 202),
        ("evt_known01", "conflict", 200),
    ]
    [conflict] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert conflict.__dict__["event_id"] == "evt_known01"
    assert conflict.__dict__["stored_fingerprint"] == stored_before["fingerprint"]
    assert conflict.__dict__["fingerprint"] not in {None, stored_before["fingerprint"]}


async def test_a_known_event_id_with_other_bytes_but_the_same_fingerprint_is_a_duplicate(
    client: httpx.AsyncClient, read_rows: ReadRows
) -> None:
    sample = razorpay_sample("payment.captured")
    body, headers = sign_razorpay(sample, event_id="evt_known01")
    await client.post("/webhooks/razorpay", content=body, headers=headers)
    sample["payload"]["payment"]["entity"]["fee"] = 3  # not in the fingerprint
    resent, resent_headers = sign_razorpay(sample, event_id="evt_known01")

    response = await client.post("/webhooks/razorpay", content=resent, headers=resent_headers)

    assert (response.status_code, response.json()) == (200, {"result": "duplicate"})
    deliveries = await read_rows("SELECT result FROM deliveries ORDER BY id")
    assert [d["result"] for d in deliveries] == ["new", "duplicate"]
