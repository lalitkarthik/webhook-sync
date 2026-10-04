"""The payer: what pays a test order for `wsync demo` (Q105, Q181).

A test payment is an order made with `POST /v1/orders` and paid through Razorpay's Checkout:
Netbanking, any bank, then Success on the mock bank page. `CheckoutPages` serves, on loopback, a
page that opens Checkout for one order. The browser payer drives headless Chromium through it;
when it can't, a person opens the same page. Playwright is in the optional `wsync[demo]` extra
and is imported only when the browser payer pays, so the service never loads a browser.

`WSYNC_DEMO_PAYER=<module>:<name>` names another payer, made from the config: the tests' fake
payer, which pays the way Checkout's page does, without a browser.
"""

import html
import importlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol, Self, override

from webhook_sync.config import Config

if TYPE_CHECKING:
    from playwright.sync_api import Page

PAYER_VARIABLE = "WSYNC_DEMO_PAYER"
CHECKOUT_JS = "https://checkout.razorpay.com/v1/checkout.js"
STEP_MS = 60_000
"""How long the browser payer waits for any one step of Checkout: its script, its frame, the
bank's page, the answer."""
BANKS = re.compile(r"^(Canara Bank|IDBI|Yes Bank|Karnataka Bank)$")
"""Banks Checkout's Netbanking tab listed in test mode (seen 2026-10-04): picking one opens the
mock bank page at once."""


class PayerFailed(Exception):
    """The payer could not pay the order; the message says why in plain words."""


@dataclass(frozen=True)
class Checkout:
    """One order to pay, and the page that opens Checkout for it."""

    order_id: str
    amount: int
    """In minor units."""
    currency: str
    page_url: str
    """A loopback page that opens Checkout for this order: the browser payer's, or a person's."""


class Payer(Protocol):
    def pay(self, checkout: Checkout) -> None:
        """Pay the order through Checkout; raise PayerFailed when it can't."""
        ...


def load_payer(config: Config, environ: Mapping[str, str]) -> Payer:
    """The payer `WSYNC_DEMO_PAYER` names, made from the config, else the browser payer."""
    named = environ.get(PAYER_VARIABLE, "")
    if not named:
        return BrowserPayer()
    module, _, name = named.partition(":")
    factory: Callable[[Config], Payer] = getattr(importlib.import_module(module), name)
    return factory(config)


class CheckoutPages:
    """Pages that open Checkout, served on 127.0.0.1 while the block runs, one per order.

    A page holds the key id, as every Checkout page does; the URLs printed never do."""

    def __init__(self, key_id: str) -> None:
        self._key_id = key_id
        self._pages: dict[str, bytes] = {}
        self._server = _PageServer(self._pages)
        self._url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> Self:
        Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()

    def add(self, order_id: str, amount: int, currency: str) -> Checkout:
        options = {
            "key": self._key_id,
            "order_id": order_id,
            "amount": amount,
            "currency": currency,
            "name": "webhook-sync demo",
            "description": f"Test-mode payment for {order_id}",
            "prefill": {"name": "Demo Payer", "email": "demo@example.com", "contact": "9000090000"},
        }
        self._pages[order_id] = _PAGE.format(
            order_id=html.escape(order_id),
            checkout_js=CHECKOUT_JS,
            options=json.dumps(options).replace("</", "<\\/"),
        ).encode()
        return Checkout(order_id, amount, currency, f"{self._url}/pay/{order_id}")


_PAGE = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Pay {order_id}</title></head>
<body>
<p>A Razorpay test-mode payment for {order_id}: choose Netbanking, any bank, then Success.</p>
<button id="pay" type="button">Pay</button>
<p id="paid"></p>
<script src="{checkout_js}"></script>
<script>
const options = {options};
options.handler = (answer) => {{
  document.getElementById("paid").textContent = "Paid: " + answer.razorpay_payment_id;
  document.body.dataset.paid = answer.razorpay_payment_id;
}};
const checkout = new Razorpay(options);
document.getElementById("pay").onclick = () => checkout.open();
checkout.open();
</script>
</body>
</html>
"""


class _PageServer(ThreadingHTTPServer):
    def __init__(self, pages: dict[str, bytes]) -> None:
        super().__init__(("127.0.0.1", 0), _PageHandler)
        self.pages = pages


class _PageHandler(BaseHTTPRequestHandler):
    server: _PageServer  # pyright: ignore[reportIncompatibleVariableOverride]

    def do_GET(self) -> None:
        page = self.server.pages.get(self.path.removeprefix("/pay/"))
        if page is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)

    @override
    def log_message(self, format: str, *args: Any) -> None:
        pass  # the scene's own lines say what happens


class BrowserPayer:
    """Headless Chromium through the order's page: Netbanking, a bank, Success."""

    def pay(self, checkout: Checkout) -> None:
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError:
            raise PayerFailed(
                "no browser: install the demo extra (uv sync --extra demo) and Chromium "
                "(uv run playwright install chromium)"
            ) from None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    _pay_on(browser.new_page(), checkout)
                finally:
                    browser.close()
        except Error as error:
            reason = (error.message or type(error).__name__).splitlines()[0]
            raise PayerFailed(f"the headless browser stopped: {reason}") from None


def _pay_on(page: Page, checkout: Checkout) -> None:
    page.set_default_timeout(STEP_MS)
    # The page's load waits for Checkout's script, which can be slow: each step below waits
    # for what it needs instead.
    page.goto(checkout.page_url, wait_until="commit")
    frame = page.frame_locator("iframe.razorpay-checkout-frame")
    frame.get_by_text("Netbanking").first.click()
    with page.expect_popup() as opened:
        frame.get_by_text(BANKS).first.click()
    bank = opened.value
    bank.get_by_role("button", name="Success").click()
    page.wait_for_function("document.body.dataset.paid")
