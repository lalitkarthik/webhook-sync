"""The scenarios' harness: the app in this process, Razorpay and Google faked through respx, and
a controlled clock (Q164).

A scenario scripts what the outside world does through `scenario.razorpay` and
`scenario.sheets`, runs the app with `scenario.running()`, settles it, and ends with
`scenario.check()`: the nine invariants. What it breaks on purpose it declares in
`scenario.on_purpose` first. The fixtures are in `conftest.py`.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx
from fakes.razorpay import FakeRazorpay
from fakes.sheets import FakeSheets

from webhook_sync.config import Config
from webhook_sync.receiver.app import create_app

NOW = datetime(2026, 10, 2, 20, 5, tzinfo=UTC)
"""After every recorded payment was created, and off the hour, so the hourly sheet check waits."""
HOOK = "https://hooks.example.test/services/T000/B000/fixture"


@dataclass
class Scenario:
    config: Config
    data_dir: Path
    razorpay: FakeRazorpay
    sheets: FakeSheets
    router: respx.MockRouter

    @asynccontextmanager
    async def running(self) -> AsyncGenerator[httpx.AsyncClient]:
        """The app, started (its lifespan run), and a client for its public routes."""
        app = create_app(self.config, self.data_dir)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://wsync.test") as c:
                yield c
