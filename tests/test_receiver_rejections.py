"""Every rejected request answered with its reason, and counted (seam 1: the HTTP app in process)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from support import razorpay_sample, sign_razorpay
from time_machine import TimeMachineFixture

pytestmark = pytest.mark.unrecorded

NINE = datetime(2026, 10, 2, 9, 0, 10, tzinfo=UTC)


async def test_the_51st_request_in_one_second_from_one_source_is_429(
    client: httpx.AsyncClient, time_machine: TimeMachineFixture
) -> None:
    time_machine.move_to(NINE, tick=False)
    body, headers = sign_razorpay(razorpay_sample("payment.captured"))

    statuses = [
        (await client.post("/webhooks/razorpay", content=body, headers=headers)).status_code
        for _ in range(51)
    ]

    assert statuses[0] == 202
    assert 429 not in statuses[:50]
    assert statuses[50] == 429


async def test_the_rate_cap_refills_at_50_a_second(
    client: httpx.AsyncClient, time_machine: TimeMachineFixture
) -> None:
    time_machine.move_to(NINE, tick=False)
    body, headers = sign_razorpay(b"{}")
    for _ in range(50):
        await client.post("/webhooks/razorpay", content=body, headers=headers)

    time_machine.shift(0.02)  # one token's worth
    refilled = await client.post("/webhooks/razorpay", content=body, headers=headers)
    capped = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert refilled.status_code == 400
    assert capped.status_code == 429
    assert capped.json() == {"rejection": "rate_capped"}


ONE_MB = 1024 * 1024


async def test_a_body_over_1_mb_is_413_too_large_even_with_another_content_type(
    client: httpx.AsyncClient,
) -> None:
    body, headers = sign_razorpay(b" " * (ONE_MB + 1))

    response = await client.post(
        "/webhooks/razorpay", content=body, headers={**headers, "content-type": "text/plain"}
    )

    assert response.status_code == 413
    assert response.json() == {"rejection": "too_large"}


async def test_a_streamed_body_over_1_mb_without_a_length_is_413_too_large(
    client: httpx.AsyncClient,
) -> None:
    _, headers = sign_razorpay(b"{}")

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(3):
            yield b" " * (ONE_MB // 2)

    response = await client.post("/webhooks/razorpay", content=chunks(), headers=headers)

    assert response.status_code == 413
    assert response.json() == {"rejection": "too_large"}


async def test_a_body_of_exactly_1_mb_is_read_and_checked(client: httpx.AsyncClient) -> None:
    body, headers = sign_razorpay(b"{}" + b" " * (ONE_MB - 2))

    response = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 400
    assert response.json() == {"rejection": "malformed"}
