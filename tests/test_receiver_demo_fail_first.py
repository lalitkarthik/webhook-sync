"""The demo fail-first switch in the HTTP app (seam 1): 500s after storing, then the normal answer.

`--demo-fail-first N` stores a new event and its job as normal, then answers 500 to its first N
attempts; each is a `deliveries` row with status 500 and a log line (A6, Q120).
"""

import hashlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from support import (
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_WEBHOOK_SECRET,
    ReadRows,
    razorpay_sample,
    sign_razorpay,
)

from webhook_sync.config import Config
from webhook_sync.receiver.app import create_app

pytestmark = pytest.mark.unrecorded


@pytest.fixture
async def demo_client(config: Config, data_dir: Path) -> AsyncIterator[httpx.AsyncClient]:
    """The public app started with `--demo-fail-first 4`."""
    app = create_app(config, data_dir, demo_fail_first=4)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://wsync.test") as c:
            yield c


async def test_the_first_four_attempts_are_500_and_the_fifth_is_the_normal_duplicate(
    demo_client: httpx.AsyncClient, read_rows: ReadRows
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_storm01")

    statuses = [
        (await demo_client.post("/webhooks/razorpay", content=body, headers=headers)).status_code
        for _ in range(5)
    ]

    assert statuses == [500, 500, 500, 500, 200]
    assert len(await read_rows("SELECT * FROM events")) == 1
    assert len(await read_rows("SELECT * FROM jobs")) == 1
    deliveries = await read_rows("SELECT event_id, result, status_code FROM deliveries ORDER BY id")
    assert [tuple(d.values()) for d in deliveries] == [
        ("evt_storm01", "new", 500),
        ("evt_storm01", "duplicate", 500),
        ("evt_storm01", "duplicate", 500),
        ("evt_storm01", "duplicate", 500),
        ("evt_storm01", "duplicate", 200),
    ]


async def test_each_event_id_has_its_own_count(
    demo_client: httpx.AsyncClient, read_rows: ReadRows
) -> None:
    first, first_headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_a01")
    second_sample = razorpay_sample("payment.failed")
    second, second_headers = sign_razorpay(second_sample, event_id="evt_b01")
    for _ in range(4):
        await demo_client.post("/webhooks/razorpay", content=first, headers=first_headers)

    answer = await demo_client.post("/webhooks/razorpay", content=second, headers=second_headers)

    assert answer.status_code == 500
    assert len(await read_rows("SELECT * FROM events")) == 2


async def test_each_attempt_logs_its_arrival_time_event_id_and_body_sha256_and_no_secret(
    demo_client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_logged01")

    with caplog.at_level(logging.INFO):
        for _ in range(5):
            await demo_client.post("/webhooks/razorpay", content=body, headers=headers)

    stored = [r for r in caplog.records if r.getMessage() == "delivery stored"]
    assert [r.__dict__["status_code"] for r in stored] == [500, 500, 500, 500, 200]
    assert [r.__dict__["attempt"] for r in stored] == [1, 2, 3, 4, 5]
    for record in stored:
        assert record.__dict__["event_id"] == "evt_logged01"
        assert record.__dict__["body_sha256"] == hashlib.sha256(body).hexdigest()
        assert record.__dict__["received_at"]
    logged = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    for secret in (
        TEST_KEY_ID,
        TEST_KEY_SECRET,
        TEST_WEBHOOK_SECRET,
        headers["x-razorpay-signature"],
    ):
        assert secret not in logged


async def test_without_the_switch_a_new_event_is_202_and_logs_no_attempt(
    client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_plain01")

    with caplog.at_level(logging.INFO):
        response = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 202
    [stored] = [r for r in caplog.records if r.getMessage() == "delivery stored"]
    assert "attempt" not in stored.__dict__
