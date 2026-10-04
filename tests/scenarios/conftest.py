"""The scenarios' fixtures: the clock, quick loops and the `scenario` (see `harness.py`)."""

from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import respx
from fakes.razorpay import RAZORPAY_API, FakeRazorpay
from fakes.sheets import FakeSheets
from scenarios.harness import HOOK, NOW, Scenario
from support import RECORDED_ACCOUNT, TEST_SPREADSHEET_ID
from time_machine import TimeMachineFixture

from webhook_sync.config import AlertsConfig, Config, SweeperConfig
from webhook_sync.queue import runner as runner_module
from webhook_sync.sinks import sheets as sheets_module
from webhook_sync.sweeper import sweep as sweeper_module


@pytest.fixture(autouse=True)
def clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(NOW, tick=True)
    return time_machine


@pytest.fixture(autouse=True)
def quick_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """No sweep start delay, and the stage runners' idle waits short, so a scenario takes
    seconds."""
    monkeypatch.setattr(sweeper_module, "START_DELAY", timedelta(0))
    monkeypatch.setattr(runner_module, "IDLE_WAIT", timedelta(milliseconds=10))
    monkeypatch.setattr(sheets_module, "FLUSH_EVERY", timedelta(milliseconds=30))


@pytest.fixture
def scenario(config: Config, data_dir: Path, clock: TimeMachineFixture) -> Iterator[Scenario]:
    """Razorpay, Google and the alerts webhook faked through one respx router, and the config
    the recordings need. The first sweep starts empty, so only what a scenario sends arrives.
    The sheet holds the buyer's own tab and cells, for invariant 6 (Q168)."""
    scenario_config = replace(
        config,
        razorpay=replace(config.razorpay, account=RECORDED_ACCOUNT, api_url=RAZORPAY_API),
        sweeper=SweeperConfig(initial_lookback_days=0),
        alerts=AlertsConfig(url=HOOK),
    )
    razorpay = FakeRazorpay(now=NOW)
    sheets = FakeSheets(TEST_SPREADSHEET_ID)
    sheets.add_buyers_own()
    with respx.mock(assert_all_called=False) as router:
        razorpay.mount(router)
        sheets.mount(router)
        router.post(HOOK).respond(200, text="ok")
        yield Scenario(scenario_config, data_dir, razorpay, sheets, router, clock)
