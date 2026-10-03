# tests/test_protect_tests.py
import io
import json

import fl_lib as fl
import protect_tests
from conftest import branch


def setup(repo):
    branch(repo, "ticket/7-x")
    return fl.load_config(repo), {"base": "main", "allow": []}


def test_existing_acceptance_test_is_denied(repo):
    cfg, t = setup(repo)
    reason = protect_tests.decide(str(repo / "tests/acceptance/test_ac.py"), repo, cfg, t)
    assert reason and "tests/acceptance/test_ac.py" in reason


def test_new_snapshot_is_allowed_but_new_gate_script_is_not(repo):
    cfg, t = setup(repo)
    assert protect_tests.decide(str(repo / "src/__snapshots__/a.ambr"), repo, cfg, t) is None
    assert protect_tests.decide(str(repo / "scripts/claude/x.py"), repo, cfg, t)


def test_source_files_and_outside_repo_are_allowed(repo, tmp_path):
    cfg, t = setup(repo)
    assert protect_tests.decide(str(repo / "src/app/core.py"), repo, cfg, t) is None
    assert protect_tests.decide(str(tmp_path / "elsewhere.py"), repo, cfg, t) is None


def test_allow_list_from_ticket(repo):
    cfg, _ = setup(repo)
    t = {"base": "main", "allow": ["tests/golden/AC-7-2/**"]}
    assert protect_tests.decide(str(repo / "tests/golden/AC-7-2/expected.json"), repo, cfg, t) is None


def test_main_prints_deny_json(repo, monkeypatch, capsys):
    setup(repo)
    (repo / ".fl-ticket.json").write_text(json.dumps({"base": "main"}))
    payload = {"cwd": str(repo), "tool_name": "Edit",
               "tool_input": {"file_path": str(repo / "tests/acceptance/test_ac.py")}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert protect_tests.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_main_is_silent_when_allowed(repo, monkeypatch, capsys):
    setup(repo)
    payload = {"cwd": str(repo), "tool_name": "Write", "tool_input": {"file_path": str(repo / "src/app/n.py")}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert protect_tests.main() == 0
    assert capsys.readouterr().out == ""


def test_test_runner_config_is_denied_even_when_new(repo):
    cfg, t = setup(repo)
    for rel in ("pytest.ini", "web/playwright.config.ts", "web/vitest.config.mts", "jest.config.js",
                "svc/.coveragerc"):
        reason = protect_tests.decide(str(repo / rel), repo, cfg, t)
        assert reason and "test-runner or coverage config" in reason, rel


def test_new_conftest_stays_editable_and_allow_exempts_test_config(repo):
    cfg, t = setup(repo)
    assert protect_tests.decide(str(repo / "tests/conftest.py"), repo, cfg, t) is None
    allowed = {"base": "main", "allow": ["pytest.ini"]}
    assert protect_tests.decide(str(repo / "pytest.ini"), repo, cfg, allowed) is None


def hook(monkeypatch, capsys, stdin):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    assert protect_tests.main() == 0
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"] if out.strip() else None


def test_broken_ticket_or_config_denies_instead_of_crashing(repo, monkeypatch, capsys):
    setup(repo)
    payload = json.dumps({"cwd": str(repo), "tool_name": "Write",
                          "tool_input": {"file_path": str(repo / "src/app/n.py")}})
    (repo / ".fl-ticket.json").write_text("{not json")
    out = hook(monkeypatch, capsys, payload)
    assert out["permissionDecision"] == "deny" and "JSONDecodeError" in out["permissionDecisionReason"]
    (repo / ".fl-ticket.json").write_text("[]")
    assert hook(monkeypatch, capsys, payload)["permissionDecision"] == "deny"
    (repo / ".fl-ticket.json").unlink()
    (repo / "scripts/claude/fl-config.json").write_text("[1")
    out = hook(monkeypatch, capsys, payload)
    assert out["permissionDecision"] == "deny" and "JSONDecodeError" in out["permissionDecisionReason"]


def test_unreadable_hook_input_denies(monkeypatch, capsys):
    assert hook(monkeypatch, capsys, "{not json")["permissionDecision"] == "deny"
