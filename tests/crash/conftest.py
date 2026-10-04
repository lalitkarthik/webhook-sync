"""The crash tests' fixtures: the `crash` run, and `crash_with_history` (see `harness.py`)."""

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from crash.harness import CrashRun
from fakes.over_http import FakesOverHttp
from fakes.razorpay import FakeRazorpay
from fakes.sheets import FakeSheets
from processes import fixture_environment, free_port, write_serve_config
from support import RECORDED_ACCOUNT, TEST_SPREADSHEET_ID

from webhook_sync.config import load_config


@contextmanager
def crash_run(data_dir: Path, razorpay: FakeRazorpay, *, lookback_days: int) -> Generator[CrashRun]:
    """A config on `data_dir` for the recordings' account, its first sweep reading
    `lookback_days` back, with `razorpay` and FakeSheets served over HTTP."""
    sheets = FakeSheets(TEST_SPREADSHEET_ID)
    with FakesOverHttp(razorpay, sheets) as fakes:
        port, admin_port = free_port(), free_port()
        config_path = write_serve_config(
            data_dir.parent,
            port,
            admin_port,
            account=RECORDED_ACCOUNT,
            api_url=fakes.url,
            extra=f"\n[sweeper]\ninitial_lookback_days = {lookback_days}\n",
        )
        environ = fakes.environ(fixture_environment())
        config = load_config(config_path, environ)
        assert config.service.data_dir == data_dir
        yield CrashRun(config, config_path, environ, port, admin_port, razorpay, sheets)


@pytest.fixture
def crash(data_dir: Path) -> Iterator[CrashRun]:
    """The first sweep starts empty, so only what a test sends arrives."""
    with crash_run(data_dir, FakeRazorpay(now=datetime.now(UTC)), lookback_days=0) as run:
        yield run


@pytest.fixture
def crash_with_history(data_dir: Path) -> Iterator[CrashRun]:
    """The first sweep reads one day back, so it walks four slices; FakeRazorpay knows no
    payment yet, and a test places the ones it lists."""
    with crash_run(data_dir, FakeRazorpay(now=datetime.now(UTC)), lookback_days=1) as run:
        yield run
