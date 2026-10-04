"""FakeRazorpay over HTTP answers as it does in process, taking its latency per call (Q167).

The Sheets side is held to Google's recordings over HTTP by `test_fake_sheets_contract.py`.
"""

import time
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest
from fakes.over_http import FakesOverHttp
from fakes.razorpay import FakeRazorpay, recorded_latency, recorded_listed_payments
from support import recorded_fetch

NOW = datetime(2026, 10, 2, 20, 5, tzinfo=UTC)
CAPTURED = "pay_Tj9rKBOXWcsMeE"
REFUNDED = "pay_Tj9gmEfed6kRqO"
LATENCY = 0.2


async def test_razorpay_over_http_answers_fetches_and_lists_as_in_process() -> None:
    razorpay = FakeRazorpay(now=NOW, latency=LATENCY)
    razorpay.payments[CAPTURED] = recorded_listed_payments()[CAPTURED]
    created = razorpay.payments[CAPTURED]["created_at"]

    with FakesOverHttp(razorpay) as fakes:
        async with httpx.AsyncClient(base_url=fakes.url, trust_env=False) as client:
            started = time.monotonic()
            fetched = await client.get(f"/v1/payments/{CAPTURED}")
            took = time.monotonic() - started
            unknown = await client.get("/v1/payments/pay_NeverMade00001")
            listed = await client.get("/v1/payments", params={"from": created, "to": created})

    assert (fetched.status_code, fetched.json()) == (200, razorpay.fetches[CAPTURED])
    assert took >= LATENCY
    assert (unknown.status_code, unknown.json()) == (400, recorded_fetch("not-found-well-formed"))
    assert [item["id"] for item in listed.json()["items"]] == [CAPTURED]
    assert listed.headers["date"] == format_datetime(NOW, usegmt=True)


async def test_razorpay_over_http_answers_a_payments_refunds_page_by_page_as_recorded(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with FakesOverHttp(FakeRazorpay(now=NOW)) as fakes:
        async with httpx.AsyncClient(base_url=fakes.url, trust_env=False) as client:
            pages = [
                await client.get(f"/v1/payments/{REFUNDED}/refunds", params=query)
                for query in ({"count": 1}, {"count": 1, "skip": 1}, {"count": 1, "skip": 2})
            ]
            unknown = await client.get("/v1/payments/pay_00000000000000/refunds?count=1")

    assert [(page.status_code, page.json()) for page in pages] == [
        (200, recorded_fetch(name))
        for name in ("refunds-page-1", "refunds-page-2", "refunds-page-3-empty")
    ]
    assert (unknown.status_code, unknown.json()) == (200, recorded_fetch("refunds-unknown-payment"))
    logged = capsys.readouterr().err
    assert f'"GET /v1/payments/{REFUNDED}/refunds?count=1&skip=1 HTTP/1.1" 200' in logged


def test_the_recorded_latency_is_the_median_recorded_call() -> None:
    assert recorded_latency() == 1.5595
