"""The `wsync` console script starts and answers --help and --version."""

import re
import shutil
import subprocess
import sys
from pathlib import Path


def run_wsync(*args: str) -> subprocess.CompletedProcess[str]:
    wsync = shutil.which("wsync", path=Path(sys.executable).parent)
    assert wsync is not None, "the wsync console script is not installed"
    return subprocess.run([wsync, *args], capture_output=True, text=True, timeout=30, check=False)


def test_wsync_help_names_the_program_and_exits_zero() -> None:
    result = run_wsync("--help")

    assert result.returncode == 0
    assert result.stdout.splitlines()[0].startswith("usage: wsync")


def test_wsync_version_prints_the_program_and_its_version() -> None:
    result = run_wsync("--version")

    assert result.returncode == 0
    assert re.fullmatch(r"wsync \d+\.\d+\.\d+\S*\n", result.stdout)
