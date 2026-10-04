"""The scenarios' harness: the app in this process, Razorpay and Google faked through respx, and
a controlled clock (Q164).

A scenario scripts what the outside world does through `scenario.razorpay` and
`scenario.sheets`, runs the app with `scenario.running()`, settles it, and ends with
`scenario.check()`: the nine invariants. What it breaks on purpose it declares in
`scenario.on_purpose` first. The fixtures are in `conftest.py`.
"""

import asyncio
import time
from collections.abc import AsyncGenerator
from typing import Any
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import respx
from fakes.razorpay import FakeRazorpay
from fakes.sheets import FakeSheets, Tab
from invariants import (
    SETTLE_LIMIT,
    LedgerWatch,
    OnPurpose,
    Outcome,
    assert_invariants,
    buyers_tabs,
    observe,
    settle,
)
from fastapi import FastAPI
from time_machine import TimeMachineFixture

from webhook_sync.admin.app import BY_HEADER, create_admin_app
from webhook_sync.clock import to_db_text
from webhook_sync.config import Config
from webhook_sync.db.reader import Reader
from webhook_sync.receiver.app import create_app

NOW = datetime(2026, 10, 2, 20, 5, tzinfo=UTC)
"""After every recorded payment was created, and off the hour, so the hourly sheet check waits."""
HOOK = "https://hooks.example.test/services/T000/B000/fixture"
SETTLE_BEHIND = timedelta(seconds=60)
"""How far behind Razorpay's now a caught-up checkpoint stands (sweeper.md)."""


@dataclass
class Scenario:
    config: Config
    data_dir: Path
    razorpay: FakeRazorpay
    sheets: FakeSheets
    router: respx.MockRouter
    clock: TimeMachineFixture
    on_purpose: OnPurpose = field(default_factory=OnPurpose)
    """What the scenario breaks on purpose: declare it before checking."""
    buyers_tabs: list[Tab] = field(default_factory=list[Tab])
    """The buyer's tabs as they stood before the app first ran."""
    watch: LedgerWatch = field(init=False)
    _app: FastAPI | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self.buyers_tabs = buyers_tabs(self.sheets)
        self.watch = LedgerWatch(self.data_dir)

    @asynccontextmanager
    async def running(self) -> AsyncGenerator[httpx.AsyncClient]:
        """The app, started (its lifespan run) and its ledger watched, and a client for its
        public routes."""
        app = create_app(self.config, self.data_dir)
        async with app.router.lifespan_context(app), self.watch:
            self._app = app
            transport = httpx.ASGITransport(app=app)
            try:
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://wsync.test"
                ) as c:
                    yield c
            finally:
                self._app = None

    @asynccontextmanager
    async def _admin(self) -> AsyncGenerator[httpx.AsyncClient]:
        assert self._app is not None, "the app is not running"
        transport = httpx.ASGITransport(app=create_admin_app(self._app, self.data_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://admin.test") as admin:
            yield admin

    async def resume(self, name: str) -> None:
        """`wsync resume <name>` on the running app, through its admin listener."""
        async with self._admin() as admin:
            answer = await admin.post(f"/admin/resume/{name}", headers={BY_HEADER: "cli"})
        assert answer.json() == {"name": name, "result": "resumed"}, answer.text

    async def status(self) -> dict[str, Any]:
        """What `wsync status` reads from the running app's admin listener."""
        async with self._admin() as admin:
            return (await admin.get("/admin/status")).json()

    def at(self, moment: datetime) -> None:
        """Our clock and Razorpay's both at `moment`, and ticking on."""
        self.clock.move_to(moment, tick=True)
        self.razorpay.now = moment

    async def caught_up(self, *, limit: float = SETTLE_LIMIT) -> None:
        """Wait until both lists' checkpoints stand at Razorpay's now less a minute, or past
        it (a first start's): every slice up to it stored, its events' jobs enqueued."""
        at = to_db_text(self.razorpay.now - SETTLE_BEHIND)
        await self._until(
            f"SELECT count(*) AS n FROM checkpoints WHERE checkpoint_at >= '{at}'",
            f"both checkpoints at {at} or past it",
            limit,
        )

    async def relisted(self, since: datetime, *, limit: float = SETTLE_LIMIT) -> None:
        """Wait until both lists have been re-listed whole at `since` or later, on our clock."""
        at = to_db_text(since)
        await self._until(
            f"SELECT count(*) AS n FROM checkpoints WHERE last_relist_at >= '{at}'",
            f"both lists re-listed since {at}",
            limit,
        )

    async def _until(self, count: str, what: str, limit: float) -> None:
        deadline = time.monotonic() + limit
        async with Reader.open(self.data_dir) as reader:
            while (await reader.fetch_all(count))[0]["n"] < 2:
                if time.monotonic() > deadline:
                    raise AssertionError(f"timed out after {limit} s waiting for {what}")
                await asyncio.sleep(0.01)

    async def settle(self, *, limit: float = SETTLE_LIMIT) -> None:
        await settle(self.data_dir, paused=self.on_purpose.paused, limit=limit)

    async def outcome(self) -> Outcome:
        return await observe(
            self.data_dir,
            self.config,
            self.razorpay,
            self.sheets,
            buyers_tabs=self.buyers_tabs,
            watch=self.watch,
        )

    async def check(self) -> None:
        """The nine invariants, over what the run left."""
        assert_invariants(await self.outcome(), self.on_purpose)
