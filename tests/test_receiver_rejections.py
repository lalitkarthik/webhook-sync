"""Every rejected request answered with its reason, and counted (seam 1: the HTTP app)."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import httpx
import pytest
from support import ReadRows, razorpay_sample, sign_razorpay
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


@pytest.mark.parametrize(
    "content_type",
    ["application/json", "application/json; charset=utf-8", "Application/JSON", "x/vnd.a+json"],
)
async def test_a_json_content_type_with_any_parameters_passes(
    client: httpx.AsyncClient, content_type: str
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"))

    response = await client.post(
        "/webhooks/razorpay", content=body, headers={**headers, "content-type": content_type}
    )

    assert response.status_code == 202


@pytest.mark.parametrize(
    "content_type", [None, "", "text/plain", "application/jsonp", "json", "application/x-json+"]
)
async def test_any_other_content_type_is_415_not_json_even_with_a_bad_signature(
    client: httpx.AsyncClient, content_type: str | None
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), secret="not-our-secret")
    del headers["content-type"]
    if content_type is not None:
        headers["content-type"] = content_type

    response = await client.post("/webhooks/razorpay", content=body, headers=headers)

    assert response.status_code == 415
    assert response.json() == {"rejection": "not_json"}


@pytest.mark.parametrize(
    "header",
    [
        "x-razorpay-signature",
        "X-Razorpay-Event-Id",
        "stripe-signature",
        "webhook-id",
        "webhook-timestamp",
        "webhook-signature",
    ],
)
async def test_a_security_header_appearing_twice_is_400_duplicate_header(
    client: httpx.AsyncClient, read_rows: ReadRows, header: str
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"))
    repeated = [(k, v) for k, v in headers.items() if k != header.lower()]
    value = headers.get(header.lower(), "v1")
    repeated += [(header, value), (header, value)]

    response = await client.post(
        "/webhooks/razorpay", content=body, headers=httpx.Headers(repeated)
    )

    assert response.status_code == 400
    assert response.json() == {"rejection": "duplicate_header"}
    assert await read_rows("SELECT * FROM events") == []


async def test_a_duplicated_security_header_is_reported_before_a_bad_signature(
    client: httpx.AsyncClient,
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), secret="not-our-secret")
    repeated = [*headers.items(), ("x-razorpay-event-id", "evt_other")]

    response = await client.post(
        "/webhooks/razorpay", content=body, headers=httpx.Headers(repeated)
    )

    assert response.json() == {"rejection": "duplicate_header"}
