"""Smoke: the verify skill's launcher writes the port files its SKILL.md commands read (#61).

Runs `wsyncv.py launch` and `cleanup` as the skill documents them, with this interpreter, so the
real `wsync serve` starts on fixture secrets against the fakes; nothing leaves 127.0.0.1.
"""

import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / ".claude" / "skills" / "verify-webhook-sync"
HELPER = SKILL / "wsyncv.py"
VERIFY = ROOT / ".verify"


def helper(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HELPER), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


@pytest.fixture
def launched() -> Iterator[subprocess.CompletedProcess[str]]:
    if (VERIFY / "pid").exists():
        pytest.skip("a verify run is live in this worktree; clean it up first")
    result = helper("launch")
    try:
        yield result
    finally:
        helper("cleanup")


def test_launch_writes_the_public_port_its_health_command_reads(
    launched: subprocess.CompletedProcess[str],
) -> None:
    assert launched.returncode == 0, launched.stdout + launched.stderr

    port = (VERIFY / "port").read_text(encoding="utf-8")

    assert re.fullmatch(r"\d+", port)
    assert httpx.get(f"http://127.0.0.1:{port}/health", timeout=5).status_code == 200


def test_every_port_file_skill_md_names_exists_after_launch(
    launched: subprocess.CompletedProcess[str],
) -> None:
    assert launched.returncode == 0, launched.stdout + launched.stderr
    skill_md = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    named = set(re.findall(r"\.verify/(?:run/)?[a-z_]*port\b", skill_md))
    assert named >= {".verify/port", ".verify/run/admin_port", ".verify/run/upstreams_port"}

    for name in named:
        assert re.fullmatch(r"\d+", (ROOT / name).read_text(encoding="utf-8")), name


def test_cleanup_leaves_the_port_file_and_removes_the_run_folder_as_skill_md_says() -> None:
    if (VERIFY / "pid").exists():
        pytest.skip("a verify run is live in this worktree; clean it up first")
    assert helper("launch").returncode == 0

    cleaned = helper("cleanup")

    assert cleaned.returncode == 0
    assert (VERIFY / "port").is_file()
    assert not (VERIFY / "run").exists()
    assert not (VERIFY / "pid").exists()
