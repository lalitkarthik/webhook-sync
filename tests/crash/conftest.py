"""The crash tests' fixture: the `crash` run (see `harness.py`)."""

from collections.abc import Iterator
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


@pytest.fixture
def crash(tmp_path: Path, data_dir: Path) -> Iterator[CrashRun]:
    """A config on `data_dir` for the recordings' account, with FakeRazorpay and FakeSheets
    served over HTTP. The first sweep starts empty, so only what a test sends arrives."""
    razorpay = FakeRazorpay(now=datetime.now(UTC))
    sheets = FakeSheets(TEST_SPREADSHEET_ID)
    with FakesOverHttp(razorpay, sheets) as fakes:
        port = free_port()
        config_path = write_serve_config(
            data_dir.parent,
            port,
            account=RECORDED_ACCOUNT,
            api_url=fakes.url,
            extra="\n[sweeper]\ninitial_lookback_days = 0\n",
        )
        environ = fakes.environ(fixture_environment())
        config = load_config(config_path, environ)
        assert config.service.data_dir == data_dir
        yield CrashRun(config, config_path, environ, port, razorpay, sheets)
