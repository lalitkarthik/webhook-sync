"""`wsync doctor` run in process (#60): the command as a person types it, its printed lines and
its exit code, against FakeRazorpay and FakeSheets through respx and a real database.

One line per check: `ok`, `warning` or `fail`, the check's name, and the reason in plain words.
"""

import asyncio
import os
import sqlite3
import stat
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from fakes.razorpay import RAZORPAY_API, WEBHOOK_URL, FakeRazorpay, listed_webhook
from fakes.sheets import FakeSheets, edit_cell
from processes import free_port, write_serve_config
from support import RECORDED_ACCOUNT, TEST_SPREADSHEET_ID, fixture_secrets

from webhook_sync.cli.doctor import network_filesystem
from webhook_sync.cli.main import main
from webhook_sync.cli.serve import start
from webhook_sync.config import load_config
from webhook_sync.db.writer import Writer
from webhook_sync.receiver.app import sheets_client
from webhook_sync.sinks.sheets import PAYMENTS, Flusher

CHECKS = ["config", "secrets", "razorpay key", "razorpay webhook", "clock", "sheet", "database"]
LIVE_KEY_ID = "rzp_live_fixtureKeyId01"
PUBLIC_URL = "https://wsync-fixture.example/webhooks/razorpay"


@dataclass
class Setup:
    config_path: Path
    razorpay: FakeRazorpay
    sheets: FakeSheets
    router: respx.MockRouter

    @property
    def database(self) -> Path:
        return self.config_path.parent / "data" / "webhook-sync.db"

    def sql(self, statement: str) -> list[tuple[object, ...]]:
        with closing(sqlite3.connect(self.database)) as db, db:
            return db.execute(statement).fetchall()


@dataclass(frozen=True)
class Report:
    exit_code: int
    lines: dict[str, tuple[str, str]]
    """Each check's state and reason, by the check's name, in the order printed."""

    def state(self, check: str) -> str:
        return self.lines[check][0]

    def says(self, check: str) -> str:
        return self.lines[check][1]

    def others_ok(self, *but: str) -> bool:
        return all(state == "ok" for check, (state, _) in self.lines.items() if check not in but)


async def _first_flush(config_path: Path) -> None:
    """One flush with no jobs: the three tabs are made and their ids stored."""
    config = load_config(config_path)
    writer = await Writer.open(config.service.data_dir)
    try:
        async with httpx.AsyncClient() as http:
            await Flusher(writer, sheets_client(config, http), config).flush([])
    finally:
        await writer.close()


@contextmanager
def healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, config_dir: str = "conf"
) -> Generator[Setup]:
    """A setup with nothing wrong: the fixture secrets in the environment, a migrated database,
    the sheet's tabs made, a webhook listed at Razorpay, whose clock is ours."""
    for name, value in fixture_secrets().items():
        monkeypatch.setenv(name, value)
    razorpay = FakeRazorpay(now=datetime.now(UTC))
    sheets = FakeSheets(TEST_SPREADSHEET_ID)
    with respx.mock(assert_all_called=False) as router:
        razorpay.mount(router)
        sheets.mount(router)
        config_path = write_serve_config(
            tmp_path / config_dir, free_port(), account=RECORDED_ACCOUNT, api_url=RAZORPAY_API
        )
        with start(config_path):
            pass
        asyncio.run(_first_flush(config_path))
        yield Setup(config_path, razorpay, sheets, router)


def doctor(on: Setup, capsys: pytest.CaptureFixture[str], *args: str) -> Report:
    capsys.readouterr()
    exit_code = main(["doctor", "--config", str(on.config_path), *args])
    lines: dict[str, tuple[str, str]] = {}
    for line in capsys.readouterr().out.splitlines():
        state, _, rest = line.partition(" ")
        check, _, says = rest.strip().partition(": ")
        lines[check] = (state, says)
    return Report(exit_code, lines)


def test_a_healthy_setup_prints_one_ok_line_per_check_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with healthy(tmp_path, monkeypatch) as on:
        report = doctor(on, capsys)

    assert list(report.lines) == CHECKS
    assert {state for state, _ in report.lines.values()} == {"ok"}
    assert report.exit_code == 0
    assert "service account" in report.says("sheet")
