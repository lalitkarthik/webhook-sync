"""The crash tests' fixtures: the `crash` run, and `crash_with_history` (see `harness.py`)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from crash.harness import CrashRun, served_run


@pytest.fixture
def crash(data_dir: Path) -> Iterator[CrashRun]:
    """The first sweep starts empty, so only what a test sends arrives."""
    with served_run(data_dir) as run:
        yield run


@pytest.fixture
def crash_with_history(data_dir: Path) -> Iterator[CrashRun]:
    """The first sweep reads one day back, so it walks four slices; FakeRazorpay knows no
    payment yet, and a test places the ones it lists."""
    with served_run(data_dir, lookback_days=1) as run:
        yield run
