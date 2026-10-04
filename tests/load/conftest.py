"""The load checks' fixtures: the `load` run, and where their numbers go (Q161, Q169).

A load check runs a real `wsync serve` with FakeRazorpay and FakeSheets over HTTP, as a crash
test does (`crash.harness.served_run`). Its counts are asserted on every run; its timing is
asserted only when the run selects the load checks by themselves (`uv run pytest -m load`), on
the development laptop, and printed otherwise; the burst runs only then (Q173). Every number a
load check reports is printed at the end of the run, under "load numbers", for P6's README.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from crash.harness import CrashRun, served_run

type Report = Callable[[str], None]

NUMBERS = pytest.StashKey[list[str]]()
NO_NIGHTLY_VERIFY = '\n[verify]\nnightly_at = "23:59"\n'


@pytest.fixture
def load(data_dir: Path) -> Iterator[CrashRun]:
    """A run on `data_dir` with FakeRazorpay and FakeSheets served over HTTP (`served_run`).
    The nightly verify is never due in a test day's hours, so only the load reaches Sheets."""
    with served_run(data_dir, extra=NO_NIGHTLY_VERIFY) as run:
        yield run


@pytest.fixture
def timing_asserted(request: pytest.FixtureRequest) -> bool:
    """Whether this run asserts the timing: only `-m load`, the load checks by themselves."""
    return request.config.getoption("markexpr") == "load"


@pytest.fixture
def only_under_load(timing_asserted: bool) -> None:
    """Skip a load check too long for the default suite unless the run is `-m load` (Q173)."""
    if not timing_asserted:
        pytest.skip("runs only under -m load (Q173)")


@pytest.fixture
def report(request: pytest.FixtureRequest) -> Report:
    """Keep a line of numbers, printed at the end of the run whether the check passed or not."""
    return request.config.stash.setdefault(NUMBERS, []).append


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    lines: list[str] = terminalreporter.config.stash.get(NUMBERS, list[str]())
    if lines:
        terminalreporter.section("load numbers")
        for line in lines:
            terminalreporter.write_line(line)
