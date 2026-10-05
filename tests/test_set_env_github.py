"""`python scripts/set_env.py github` (Q176, A3): copies the weekly job's secrets from `.env` to
the repository's Actions secrets with `gh secret set`, each value on standard input. It prints
names only, and refuses a Razorpay key that is not a test key before `gh` is called once.

`gh` is a fake on `PATH` that records each call's arguments and standard input.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "set_env.py"
VALUES = {
    "RAZORPAY_KEY_ID": "rzp_test_fakekey0001",
    "RAZORPAY_KEY_SECRET": "fake-key-secret-0002",
    "RAZORPAY_WEBHOOK_SECRET": "fake-webhook-secret-0003",
    "GOOGLE_SERVICE_ACCOUNT_B64": "ZmFrZS1zZXJ2aWNlLWFjY291bnQtMDAwNA==",
    "SHEET_ID": "fake-sheet-id-0005",
}
NOT_COPIED = {"FIRECRAWL_API_KEY": "fake-firecrawl-0006"}
LIVE_KEY = "rzp_" + "live_fakekey0007"
"""Split so the repository holds no text that reads as a live key."""
FAKE_GH = """\
import json, os, sys
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"args": sys.argv[1:], "stdin": sys.stdin.read()}) + "\\n")
sys.exit(int(os.environ.get("FAKE_GH_EXIT", "0")))
"""


def _fake_gh(bin_dir: Path) -> None:
    bin_dir.mkdir()
    (bin_dir / "fake_gh.py").write_text(FAKE_GH, encoding="utf-8")
    if sys.platform == "win32":
        (bin_dir / "gh.cmd").write_text(
            f'@"{sys.executable}" "%~dp0fake_gh.py" %*\r\n', encoding="utf-8"
        )
    else:
        gh = bin_dir / "gh"
        gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/fake_gh.py" "$@"\n')
        gh.chmod(0o755)


def _run(
    tmp_path: Path, values: dict[str, str], *, gh_exit: int = 0
) -> tuple[int, str, list[dict[str, Any]]]:
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    _fake_gh(bin_dir)
    log = tmp_path / "gh.log"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "github"],
        env=os.environ
        | {
            "WSYNC_ENV_FILE": str(env_file),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_EXIT": str(gh_exit),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    calls: list[dict[str, Any]] = (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )
    return result.returncode, result.stdout + result.stderr, calls


def _shown(output: str, values: dict[str, str]) -> list[str]:
    return [name for name, value in values.items() if value and value in output]


def test_sets_each_secret_from_standard_input_and_prints_names_only(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, VALUES | NOT_COPIED)

    assert code == 0, output
    assert [call["args"] for call in calls] == [
        ["secret", "set", name, "--repo", "lalitkarthik/webhook-sync"] for name in VALUES
    ]
    assert {call["args"][2]: call["stdin"] for call in calls} == VALUES
    assert _shown(output, VALUES | NOT_COPIED) == []
    for name in VALUES:
        assert name in output
    assert "FIRECRAWL_API_KEY" not in output


@pytest.mark.parametrize("key_id", [LIVE_KEY, "fakekey0008"])
def test_refuses_a_razorpay_key_that_is_not_a_test_key(tmp_path: Path, key_id: str) -> None:
    code, output, calls = _run(tmp_path, VALUES | {"RAZORPAY_KEY_ID": key_id})

    assert code != 0
    assert calls == []
    assert "rzp_test_" in output
    assert key_id not in output
    assert _shown(output, VALUES) == []


def test_refuses_naming_a_secret_missing_from_env(tmp_path: Path) -> None:
    values = {k: v for k, v in VALUES.items() if k != "SHEET_ID"} | {"RAZORPAY_WEBHOOK_SECRET": ""}

    code, output, calls = _run(tmp_path, values)

    assert code != 0
    assert calls == []
    assert "SHEET_ID" in output
    assert "RAZORPAY_WEBHOOK_SECRET" in output
    assert _shown(output, values) == []


def test_a_refused_secret_set_fails_the_command_naming_it(tmp_path: Path) -> None:
    code, output, calls = _run(tmp_path, VALUES, gh_exit=1)

    assert code != 0
    assert [call["args"][2] for call in calls] == ["RAZORPAY_KEY_ID"]
    assert "RAZORPAY_KEY_ID" in output
    assert _shown(output, VALUES) == []
