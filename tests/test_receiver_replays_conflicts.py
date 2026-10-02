"""Replays, conflicts and the account check (seam 1: the HTTP app in process)."""

import hashlib
import json
import logging

import httpx
import pytest
from support import RAZORPAY_SAMPLES, TEST_ACCOUNT, ReadRows, razorpay_sample, sign_razorpay

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


async def test_a_body_from_another_account_is_400_wrong_account_and_stores_nothing(
    client: httpx.AsyncClient, read_rows: ReadRows
) -> None:
    sample = razorpay_sample("payment.captured")
    sample["account_id"] = "acc_SomeoneElse0001"
    body, headers = sign_razorpay(sample)

    response = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 400
    assert response.json() == {"rejection": "wrong_account"}
    assert await read_rows("SELECT * FROM events") == []
    assert await read_rows("SELECT * FROM deliveries") == []


async def test_a_body_without_an_account_is_stored_under_the_configured_one_with_a_warning(
    client: httpx.AsyncClient, read_rows: ReadRows, caplog: pytest.LogCaptureFixture
) -> None:
    sample = razorpay_sample("payment.captured")
    del sample["account_id"]
    body, headers = sign_razorpay(sample, event_id="evt_noaccount01")

    with caplog.at_level(logging.INFO):
        response = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 202
    assert await read_rows("SELECT event_id, account FROM events") == [
        {"event_id": "evt_noaccount01", "account": TEST_ACCOUNT}
    ]
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning.__dict__["event_id"] == "evt_noaccount01"


async def test_an_event_without_a_top_level_created_at_is_stored_with_no_source_time(
    client: httpx.AsyncClient, read_rows: ReadRows
) -> None:
    # The documented refund.speed_changed sample keeps its created_at inside `payload` (G61).
    body, headers = sign_razorpay(razorpay_sample("refund.speed_changed"))

    response = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 202
    [event] = await read_rows("SELECT * FROM events")
    assert event["source_created_at"] is None
    assert event["payment_key"] == "pay_EcPJsxu8cSzOK6"
    # Q117: type | primary entity id | top-level created_at, empty here | mode
    expected = "refund.speed_changed|rfnd_EcPN8eJuzH5Yaz||test"
    assert event["fingerprint"] == hashlib.sha256(expected.encode()).hexdigest()
    assert len(await read_rows("SELECT * FROM jobs")) == 1


# Q119 / A5: everything outside these is dropped before storing.
TOP_LEVEL_KEYS = {"entity", "account_id", "event", "contains", "created_at", "payload"}
ENTITY_FIELDS = {
    "id",
    "entity",
    "amount",
    "amount_refunded",
    "currency",
    "status",
    "refund_status",
    "captured",
    "order_id",
    "payment_id",
    "method",
    "speed_requested",
    "speed_processed",
    "error_code",
    "error_reason",
    "created_at",
}
SAMPLES = sorted(p.stem for p in RAZORPAY_SAMPLES.glob("*.json"))


@pytest.mark.parametrize("event_type", SAMPLES)
async def test_the_stored_payload_of_every_documented_sample_keeps_only_allow_listed_fields(
    client: httpx.AsyncClient, read_rows: ReadRows, event_type: str
) -> None:
    sample = razorpay_sample(event_type)
    body, headers = sign_razorpay(sample)

    await client.post("/webhooks/razorpay", content=body, headers=headers)

    [event] = await read_rows("SELECT payload FROM events")
    payload = json.loads(event["payload"])
    assert set(payload) <= TOP_LEVEL_KEYS
    assert payload["event"] == event_type
    entities = payload["payload"]
    assert set(entities) == set(sample["contains"])
    for name, wrapper in entities.items():
        assert set(wrapper) == {"entity"}
        entity = wrapper["entity"]
        assert set(entity) <= ENTITY_FIELDS, name
        assert entity["id"] == sample["payload"][name]["entity"]["id"]
        assert not {"email", "contact", "notes", "card", "vpa", "bank"} & set(entity)


async def test_the_delivery_log_names_the_job_of_a_new_handled_event(
    client: httpx.AsyncClient, read_rows: ReadRows, caplog: pytest.LogCaptureFixture
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_jobid01")

    with caplog.at_level(logging.INFO):
        await client.post("/webhooks/razorpay", content=body, headers=headers)

    [job] = await read_rows("SELECT id FROM jobs")
    [stored] = records(caplog, "delivery stored")
    assert stored.__dict__["job_id"] == job["id"]
