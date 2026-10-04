"""The crash tests' harness: a real `wsync serve` killed at a crash point, and a new one started
on the same database (Q159, Q164).

`crash.server(crash_at=...)` starts a server armed with `WSYNC_CRASH_AT`, against FakeRazorpay
and FakeSheets over HTTP (`fakes.over_http`); the test drives it through `server.client`, as
Razorpay does, and through `server.admin`, its admin listener, asks for a sweep by hand;
`server.crashed()` fails unless the process exited with the crash code: a
crash test that never crashed proves nothing (A15). `crash.server()` then starts a new server
on the same database; the test settles it and, while it still runs, reads what it left and ends
with `crash.check()`, the nine invariants. Not after it stops: on Windows `stop` returns once the
`wsync.exe` launcher has died, and the server under it may still hold the database's files, so a
read then fails with a disk I/O error. The fixture is in `conftest.py`.
"""

import asyncio
import subprocess
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakes.razorpay import FakeRazorpay
from fakes.sheets import FakeSheets, Tab
from invariants import (
    SETTLE_LIMIT,
    LedgerWatch,
    OnPurpose,
    assert_invariants,
    buyers_tabs,
    observe,
    settle,
)
from processes import stop, wait_until_healthy, wsync_path

from webhook_sync.config import Config
from webhook_sync.crashpoints import CRASH_EXIT_CODE, VARIABLE

CRASH_WAIT = 15.0
"""Real seconds a server armed at a crash point gets to reach it, once driven there."""


@dataclass
class Server:
    """One `wsync serve` process, and a client for its public routes."""

    process: subprocess.Popen[bytes]
    client: httpx.AsyncClient
    admin: httpx.AsyncClient
    log: Path

    async def sweep(self) -> str:
        """Ask for a sweep by hand; its run id."""
        asked = await self.admin.post("/admin/sweep")
        assert asked.status_code == 202, asked.text
        return asked.json()["run_id"]

    async def run_ended(self, run_id: str, *, within: float = SETTLE_LIMIT) -> dict[str, Any]:
        """Wait for a run by hand to end; its report."""
        deadline = time.monotonic() + within
        while True:
            report = (await self.admin.get(f"/admin/runs/{run_id}")).json()
            if report["state"] != "running":
                return report
            if time.monotonic() > deadline:
                pytest.fail(f"run {run_id} still running {within} s on\n{self._log_tail()}")
            await asyncio.sleep(0.05)

    async def crashed(self, *, within: float = CRASH_WAIT) -> None:
        """Wait for the process to exit; fail unless it exited with the crash code."""
        try:
            code = await asyncio.to_thread(self.process.wait, within)
        except subprocess.TimeoutExpired:
            pytest.fail(
                f"wsync serve still running {within} s on: the crash point was never reached"
                f"\n{self._log_tail()}"
            )
        if code != CRASH_EXIT_CODE:
            pytest.fail(
                f"wsync serve exited {code}, not with the crash code {CRASH_EXIT_CODE}"
                f"\n{self._log_tail()}"
            )

    def _log_tail(self) -> str:
        return "\n".join(self.log.read_text(encoding="utf-8").splitlines()[-20:])


@dataclass
class CrashRun:
    config: Config
    config_path: Path
    environ: dict[str, str]
    port: int
    admin_port: int
    razorpay: FakeRazorpay
    sheets: FakeSheets
    on_purpose: OnPurpose = field(default_factory=OnPurpose)
    """What the test breaks on purpose: declare it before checking."""
    buyers_tabs: list[Tab] = field(default_factory=list[Tab])
    """The buyer's tabs as they stood before the first server ran."""
    watch: LedgerWatch = field(init=False)
    _started: int = 0

    def __post_init__(self) -> None:
        self.buyers_tabs = buyers_tabs(self.sheets)
        self.watch = LedgerWatch(self.config.service.data_dir)

    @asynccontextmanager
    async def server(self, *, crash_at: str | None = None) -> AsyncGenerator[Server]:
        """A `wsync serve` on the run's database, started and healthy, armed at `crash_at`
        if given, its ledger watched; stopped at the end unless it has exited."""
        self._started += 1
        environ = self.environ | ({VARIABLE: crash_at} if crash_at else {})
        log = self.config_path.parent / f"serve-{self._started}.log"
        base_url = f"http://127.0.0.1:{self.port}"
        with log.open("wb") as log_file:
            process = await asyncio.to_thread(
                subprocess.Popen,
                [wsync_path(), "serve", "--config", str(self.config_path)],
                env=environ,
                stdout=log_file,
                stderr=log_file,
            )
            try:
                await asyncio.to_thread(wait_until_healthy, base_url, process)
                admin_url = f"http://127.0.0.1:{self.admin_port}"
                async with (
                    self.watch,
                    httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=30) as client,
                    httpx.AsyncClient(base_url=admin_url, trust_env=False, timeout=30) as admin,
                ):
                    yield Server(process, client, admin, log)
            finally:
                if process.poll() is None:
                    await asyncio.to_thread(stop, process)

    async def settle(self, *, limit: float = SETTLE_LIMIT) -> None:
        await settle(self.config.service.data_dir, paused=self.on_purpose.paused, limit=limit)

    async def check(self) -> None:
        """The nine invariants, over what the runs left."""
        outcome = await observe(
            self.config.service.data_dir,
            self.config,
            self.razorpay,
            self.sheets,
            buyers_tabs=self.buyers_tabs,
            watch=self.watch,
        )
        assert_invariants(outcome, self.on_purpose)
