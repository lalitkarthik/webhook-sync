"""Helpers shared by tests: fixture secrets, Razorpay's documented samples, and signing.

Import with `from support import ...` (pytest and pyright both put `tests/` on the path).
Fixture secrets only: never the real environment's values.
"""

import asyncio
import base64
import functools
import hashlib
import hmac
import json
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from pathlib import Path
from typing import Any

TEST_KEY_ID = "rzp_test_fixtureKeyId01"
TEST_KEY_SECRET = "fixture-key-secret-not-real"
TEST_WEBHOOK_SECRET = "fixture-webhook-secret-not-real"
TEST_ACCOUNT = "acc_BFQ7uQEaa7j2z7"
UNREACHABLE_API = "http://127.0.0.1:9"
"""A Razorpay API address nothing answers (the discard port): a fetch there fails to connect."""

RAZORPAY_SAMPLES = Path(__file__).parent / "fixtures" / "razorpay"

type ReadRows = Callable[[str], Awaitable[list[dict[str, Any]]]]


def razorpay_sample(event_type: str) -> dict[str, Any]:
    """Razorpay's documented sample body for an event type, as a dict.

    The files in tests/fixtures/razorpay are copied verbatim from razorpay.com/docs/webhooks
    (payments, refunds, disputes, orders); tests using them are marked `unrecorded` (Q113) until
    P0's recordings land. The top-level `account_id` is set to TEST_ACCOUNT, the configured account.
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


TEST_SPREADSHEET_ID = "fixture-spreadsheet-id-0001"


def fixture_secrets() -> dict[str, str]:
    """The secret variables, set to the fixture secrets: Razorpay's three and a made-up
    service-account key."""
    return {
        "RAZORPAY_KEY_ID": TEST_KEY_ID,
        "RAZORPAY_KEY_SECRET": TEST_KEY_SECRET,
        "RAZORPAY_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET,
        "GOOGLE_SERVICE_ACCOUNT_B64": fixture_service_account_b64(),
    }


@functools.cache
def fixture_service_account() -> dict[str, str]:
    """A service-account key shaped as Google issues one, with a private key made up here: it
    authenticates nowhere."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return {
        "type": "service_account",
        "project_id": "fixture-project",
        "private_key_id": "fixture0000000000000000000000000000000000",
        "private_key": pem,
        "client_email": "wsync-fixture@fixture-project.iam.gserviceaccount.com",
        "client_id": "100000000000000000000",
        "token_uri": "https://oauth2.googleapis.com/token",
    }


def fixture_service_account_b64(**changes: str | None) -> str:
    """The fixture key as `GOOGLE_SERVICE_ACCOUNT_B64` holds it; `changes` replace (or, given
    None, drop) its fields."""
    info: dict[str, str] = {
        k: v for k, v in (fixture_service_account() | changes).items() if v is not None
    }
    return base64.b64encode(json.dumps(info).encode()).decode()


RAZORPAY_RECORDINGS = Path(__file__).parent / "recordings" / "razorpay"
RECORDED_ACCOUNT = "acc_Tj5AnsI7BH2aLR"
"""The account every P0 delivery and P2 fetch answer was recorded from (G59)."""


def recorded_delivery(event_id: str, attempt: int = 1) -> bytes:
    """The raw body of a delivery P0 recorded (`tests/recordings/razorpay/<id>-<n>.json`).

    Re-sign it with `sign_razorpay(body, event_id=...)`: the recording's own signature used the
    real webhook secret, which tests never have. A test using it needs the config's account set
    to RECORDED_ACCOUNT.
    """
    recording = json.loads((RAZORPAY_RECORDINGS / f"{event_id}-{attempt}.json").read_text())
    return recording["body"].encode()


def recorded_fetch(name: str) -> dict[str, Any]:
    """The body of a recorded fetch answer, `tests/recordings/razorpay/fetch/<name>.json`."""
    return json.loads((RAZORPAY_RECORDINGS / "fetch" / f"{name}.json").read_text())["body"]


def recorded_api_answer(name: str) -> dict[str, Any]:
    """The body of a P0 API answer, `tests/recordings/razorpay/api/<name>.json`."""
    return json.loads((RAZORPAY_RECORDINGS / "api" / f"{name}.json").read_text())["body"]


async def execute_aside(data_dir: Path, sql: str) -> None:
    """Run one statement on a connection of its own, off the event loop.

    A running app's stage runners write too: a blocking connection on the loop would wait for
    the lock a runner holds while the runner waits for the loop.
    """

    def run() -> None:
        with closing(sqlite3.connect(data_dir / "webhook-sync.db")) as connection, connection:
            connection.execute(sql)

    await asyncio.to_thread(run)
