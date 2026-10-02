"""Every rejected request answered with its reason, and counted (seam 1: the HTTP app in process)."""

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
