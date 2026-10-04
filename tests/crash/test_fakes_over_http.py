"""FakeRazorpay over HTTP answers as it does in process, taking its latency per call (Q167).

The Sheets side is held to Google's recordings over HTTP by `test_fake_sheets_contract.py`.
"""

import time
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
from fakes.over_http import FakesOverHttp
from fakes.razorpay import FakeRazorpay, recorded_latency, recorded_listed_payments
from support import recorded_fetch

NOW = datetime(2026, 10, 2, 20, 5, tzinfo=UTC)
CAPTURED = "pay_Tj9rKBOXWcsMeE"
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


def test_the_recorded_latency_is_the_median_recorded_call() -> None:
    assert recorded_latency() == 1.5595
