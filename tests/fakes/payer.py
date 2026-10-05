"""Fake payers for `wsync demo`, named by `WSYNC_DEMO_PAYER` (Q181): no browser, no outside call.

`FakePayer` pays as Checkout's page does once a person picks Success, by FakeRazorpay's
checkout call; `FailingPayer` fails as a headless run does at a captcha, so `demo` falls back to
a person paying at the order's page.
"""

import httpx

from webhook_sync.config import Config
from webhook_sync.demo.payer import Checkout, PayerFailed

FAKE_PAYER = "fakes.payer:FakePayer"
FAILING_PAYER = "fakes.payer:FailingPayer"


class FakePayer:
    def __init__(self, config: Config) -> None:
        self._api_url = config.razorpay.api_url

    def pay(self, checkout: Checkout) -> None:
        answer = httpx.post(
            f"{self._api_url}/v1/payments/create/checkout",
            data={"order_id": checkout.order_id},
            timeout=10,
        )
        answer.raise_for_status()


class FailingPayer:
    def __init__(self, config: Config) -> None:
        pass

    def pay(self, checkout: Checkout) -> None:
        raise PayerFailed("Checkout showed a captcha")
