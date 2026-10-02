"""Helpers shared by tests: fixture secrets, Razorpay's documented samples, and signing.

Import with `from support import ...` (pytest and pyright both put `tests/` on the path).
Fixture secrets only: never the real environment's values.
"""

import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

TEST_KEY_ID = "rzp_test_fixtureKeyId01"
TEST_KEY_SECRET = "fixture-key-secret-not-real"
TEST_WEBHOOK_SECRET = "fixture-webhook-secret-not-real"
TEST_ACCOUNT = "acc_BFQ7uQEaa7j2z7"

RAZORPAY_SAMPLES = Path(__file__).parent / "fixtures" / "razorpay"

type ReadRows = Callable[[str], Awaitable[list[dict[str, Any]]]]


def razorpay_sample(event_type: str) -> dict[str, Any]:
    """Razorpay's documented sample body for an event type, as a dict.

    The files in tests/fixtures/razorpay are copied verbatim from razorpay.com/docs/webhooks
    (payments, disputes, orders); tests using them are marked `unrecorded` (Q113) until P0's
    recordings land. The top-level `account_id` is set to TEST_ACCOUNT, the configured account.
    """
    body: dict[str, Any] = json.loads((RAZORPAY_SAMPLES / f"{event_type}.json").read_text())
    body["account_id"] = TEST_ACCOUNT
    return body


def sign_razorpay(
    body: dict[str, Any] | bytes,
    *,
    event_id: str = "evt_fixture0000001",
    secret: str = TEST_WEBHOOK_SECRET,
) -> tuple[bytes, dict[str, str]]:
    """Sign a body as Razorpay does: hex HMAC-SHA256 of the raw bytes; add the event id header.

    `body` is a dict (serialised to JSON) or raw bytes. Returns the raw body and the headers.
    """
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    headers = {
        "content-type": "application/json",
        "x-razorpay-signature": signature,
        "x-razorpay-event-id": event_id,
    }
    return raw, headers
