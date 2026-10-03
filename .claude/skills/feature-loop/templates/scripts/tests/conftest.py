# tests/conftest.py
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


def commit(repo, files, msg="change"):
    for rel, text in files.items():
        p = Path(repo) / rel
        if text is None:
            git(repo, "rm", "-q", rel)
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
        git(repo, "add", rel)
    git(repo, "commit", "-q", "-m", msg)


def branch(repo, name):
    git(repo, "checkout", "-q", "-b", name)


def write_config(repo, **keys):
    commit(repo, {"scripts/claude/fl-config.json": json.dumps(keys)}, "config")


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    git(r, "config", "user.email", "t@example.com")
    git(r, "config", "user.name", "t")
    git(r, "config", "core.autocrlf", "false")
    commit(r, {
        "scripts/claude/fl-config.json": "{}",
        "src/app/core.py": "def f():\n    return 1\n",
        "src/other/x.py": "X = 1\n",
        "tests/test_core.py": "def test_a():\n    pass\n\ndef test_b():\n    pass\n",
        "tests/acceptance/test_ac.py": "def test_AC_7_1_flow():\n    assert False\n",
        "tests/golden/AC-7-2/expected.json": "{}\n",
    }, "base")
    return r
