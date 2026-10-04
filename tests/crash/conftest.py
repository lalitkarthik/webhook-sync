"""The crash tests' fixture: the `crash` run (see `harness.py`)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from crash.harness import CrashRun, served_run


@pytest.fixture
def crash(data_dir: Path) -> Iterator[CrashRun]:
    """A run on `data_dir` with FakeRazorpay and FakeSheets served over HTTP (`served_run`)."""
    with served_run(data_dir) as run:
        yield run
