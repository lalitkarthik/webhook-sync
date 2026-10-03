"""Fixtures shared by every test.

Each test that touches the database gets a fresh data directory and real SQLite;
time is moved with the `time_machine` fixture, never slept. Add shared helpers below,
one fixture per block, so parallel tickets append rather than edit the same lines.
Plain helpers (fixture secrets, Razorpay samples, signing) live in `support.py`.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from support import (
    TEST_ACCOUNT,
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_SPREADSHEET_ID,
    TEST_WEBHOOK_SECRET,
    UNREACHABLE_API,
    ReadRows,
    fixture_service_account,
)

from webhook_sync.config import (
    Config,
    GoogleSecrets,
    RazorpayConfig,
    RazorpaySecrets,
    ServiceConfig,
    SheetsConfig,
)
from webhook_sync.db.migrations import apply_pending
from webhook_sync.db.reader import Reader
from webhook_sync.db.writer import Writer
from webhook_sync.receiver.app import create_app


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A fresh, empty data directory for this test."""
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def config(data_dir: Path) -> Config:
    """A minimal test-mode config with fixture secrets; its data directory is `data_dir`.

    Its Razorpay API address is unreachable, so an app's ledger runner never calls Razorpay: a
    test that fetches points `api_url` at a respx-faked address."""
    return Config(
        service=ServiceConfig(
            public_listen="127.0.0.1:8080", data_dir=data_dir, timezone=ZoneInfo("Asia/Kolkata")
        ),
        razorpay=RazorpayConfig(mode="test", account=TEST_ACCOUNT, api_url=UNREACHABLE_API),
        razorpay_secrets=RazorpaySecrets(
            key_id=TEST_KEY_ID,
            key_secret=TEST_KEY_SECRET,
            webhook_secrets=(TEST_WEBHOOK_SECRET,),
        ),
        google_secrets=GoogleSecrets(service_account=fixture_service_account()),
        sheets=SheetsConfig(spreadsheet_id=TEST_SPREADSHEET_ID),
        sinks=("sheets",),
    )


@pytest.fixture
async def client(config: Config, data_dir: Path) -> AsyncIterator[httpx.AsyncClient]:
    """The public app, started (its lifespan run) and driven in process over ASGI."""
    app = create_app(config, data_dir)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://wsync.test") as c:
            yield c


@pytest.fixture
def read_rows(data_dir: Path) -> ReadRows:
    """Run a query through the read-only connection and return its rows as dicts."""

    async def read(sql: str) -> list[dict[str, Any]]:
        async with Reader.open(data_dir) as reader:
            return await reader.fetch_all(sql)

    return read


@pytest.fixture
async def writer(data_dir: Path) -> AsyncIterator[Writer]:
    """The writer on a fresh database in `data_dir`, migrated to the current schema."""
    opened = await Writer.open(data_dir)
    try:
        await apply_pending(opened)
        yield opened
    finally:
        await opened.close()
