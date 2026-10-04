"""The load checks' fixtures: the `load` run, and where their numbers go (Q161, Q169).

A load check runs a real `wsync serve` with FakeRazorpay and FakeSheets over HTTP, as a crash
test does (`crash.harness.served_run`). Its counts are asserted on every run; its timing is
asserted only when the run selects the load checks by themselves (`uv run pytest -m load`), on
the development laptop, and printed otherwise. Every number a load check reports is printed at
the end of the run, under "load numbers", for P6's README.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from crash.harness import CrashRun, served_run

type Report = Callable[[str], None]

NUMBERS = pytest.StashKey[list[str]]()


@pytest.fixture
def load(data_dir: Path) -> Iterator[CrashRun]:
    """A run on `data_dir` with FakeRazorpay and FakeSheets served over HTTP (`served_run`)."""
    with served_run(data_dir) as run:
        yield run


@pytest.fixture
def timing_asserted(request: pytest.FixtureRequest) -> bool:
    """Whether this run asserts the timing: only `-m load`, the load checks by themselves."""
    return request.config.getoption("markexpr") == "load"


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
