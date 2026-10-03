# tests/test_verifier_sandbox.py
import io
import json

import pytest

import verifier_sandbox as vs

ALLOWED = [
    "uv run pytest tests/acceptance -q -k AC_7_1",
    "playwright-cli -s=wt7 snapshot > /c/repo/verification/7/abc1234/AC-7-1.yml",
    "mkdir -p verification/7/abc1234 .verify",
    "curl -s localhost:3412/health 2>/dev/null",
    "cat out.json 2>&1 | head -40",
    "cp /tmp/shot.png \"$EVID/AC-7-1.png\"",
    "gh pr view 3 --json headRefOid",
    "git log --oneline -5",
]
DENIED = [
    "git commit -am x",
    "git checkout -- src",
    "echo hi > src/app.py",
    "rm -rf src",
    "sed -i s/a/b/ src/x.py",
    "npm install left-pad",
    "uv add requests",
    "gh pr merge 3 --squash",
    "gh api repos/a/b/statuses/abc -f state=success",
    "tee src/notes.txt < x",
]


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allowed(cmd):
    assert vs.decide(cmd) is None


@pytest.mark.parametrize("cmd", DENIED)
def test_denied(cmd):
    assert vs.decide(cmd)


def test_main_denies_with_json(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_input": {"command": "git push"}})))
    assert vs.main() == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("stdin", ["{not json", json.dumps({"tool_input": {"command": 5}}), json.dumps([1])])
def test_main_denies_instead_of_crashing(stdin, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert vs.main() == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "deny"
