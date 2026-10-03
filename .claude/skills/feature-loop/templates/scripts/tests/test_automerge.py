# tests/test_automerge.py
import json
import os
import sys
from pathlib import Path

import pytest

import automerge
import fl_lib as fl
from conftest import branch, commit, git
from test_verdict import good

FAKE = Path(__file__).resolve().parent / "fake_gh.py"


GREEN = json.dumps([{"name": "ci", "bucket": "pass"}, {"name": "verify/user-flow", "bucket": "pass"}])


def setup(repo, tmp_path, monkeypatch, files=None, automerge_var=(1, ""), head=None, verdict_sha=None,
          first=(), checks=(0, GREEN), rulesets=(0, "0"), state=(0, "MERGED")):
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    branch(repo, "feature/x")
    commit(repo, files or {"src/app/core.py": "def f():\n    return 7\n"})
    sha = fl.git(["rev-parse", "HEAD"], repo).strip()
    log = tmp_path / "gh.log"
    rules = [
        *first,
        [["variable", "get", "AUTOMERGE"], automerge_var[0], automerge_var[1]],
        [["pr", "view"], state[0], state[1], ["state"]],
        [["pr", "view"], 0, json.dumps({"headRefOid": head or sha, "baseRefName": "main", "title": "T"})],
        [["repo", "view"], 0, "me/r"],
        [["api", "repos/me/r/rulesets"], rulesets[0], rulesets[1]],
        [["pr", "checks"], checks[0], checks[1]],
    ]
    monkeypatch.setenv("FL_GH", f'"{Path(sys.executable).as_posix()}" "{FAKE.as_posix()}"')
    monkeypatch.setenv("FAKE_GH_LOG", str(log))
    monkeypatch.setenv("FAKE_GH_RULES", json.dumps(rules))
    ev = tmp_path / "ev"
    (ev / "verdict.json").parent.mkdir(parents=True, exist_ok=True)
    (ev / "verdict.json").write_text(json.dumps(good(ev, sha=verdict_sha or sha)))
    return sha, log, ev / "verdict.json"


def calls(log):
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def run(repo, verdict, *extra):
    return automerge.main(["--pr", "5", "--verdict", str(verdict), "--root", str(repo), *extra])


def merged(log):
    return [c for c in calls(log) if c[:2] == ["pr", "merge"]]


def test_merges_with_status_pinned_to_head(repo, tmp_path, monkeypatch, capsys):
    sha, log, v = setup(repo, tmp_path, monkeypatch)
    assert run(repo, v) == 0
    assert capsys.readouterr().out.strip() == f"MERGED PR #5 at {sha[:7]}"
    c = calls(log)
    kinds = [x[:2] for x in c]
    assert kinds.index(["api", f"repos/me/r/statuses/{sha}"]) < kinds.index(["pr", "checks"]) \
        < kinds.index(["pr", "merge"])
    assert ["api", f"repos/me/r/statuses/{sha}", "-f", "state=success", "-f", "context=verify/user-flow",
            "-f", "description=1/1 AC PASS"] in c
    assert ["pr", "merge", "5", "--squash", "--delete-branch", "--match-head-commit", sha] in c


def test_auto_flag_arms_instead(repo, tmp_path, monkeypatch):
    sha, log, v = setup(repo, tmp_path, monkeypatch)
    assert run(repo, v, "--auto") == 0
    assert ["pr", "merge", "5", "--squash", "--delete-branch", "--match-head-commit", sha, "--auto"] in calls(log)


def test_kill_switch(repo, tmp_path, monkeypatch):
    _, log, v = setup(repo, tmp_path, monkeypatch, automerge_var=(0, "off"))
    assert run(repo, v) == 4
    assert not any(c[:2] == ["pr", "merge"] for c in calls(log))


def test_hold_opens_issue_and_approve_overrides(repo, tmp_path, monkeypatch):
    _, log, v = setup(repo, tmp_path, monkeypatch, files={".github/workflows/x.yml": "on: push\n"})
    assert run(repo, v) == 3
    c = calls(log)
    assert any(x[:2] == ["issue", "create"] and "ready-for-human" in x for x in c)
    body = next(x for x in c if x[:2] == ["issue", "create"])
    body = body[body.index("--body") + 1]
    assert "--approve" in body and "run the same command again" in body
    assert not any(x[:2] == ["pr", "merge"] for x in c)
    assert run(repo, v, "--approve") == 0
    assert ["pr", "edit", "5", "--add-label", "human-approved"] in calls(log)


def test_stale_verdict_refused(repo, tmp_path, monkeypatch):
    _, log, v = setup(repo, tmp_path, monkeypatch, verdict_sha="0" * 40)
    assert run(repo, v) == 1
    assert not any(c[:2] == ["pr", "merge"] for c in calls(log))


def test_checkout_must_be_at_pr_head(repo, tmp_path, monkeypatch):
    _, log, v = setup(repo, tmp_path, monkeypatch, head="f" * 40)
    assert run(repo, v) == 1


def test_failing_checks_block_a_direct_merge(repo, tmp_path, monkeypatch, capsys):
    red = json.dumps([{"name": "ci", "bucket": "fail"}, {"name": "lint", "bucket": "cancel"},
                      {"name": "verify/user-flow", "bucket": "pass"}])
    _, log, v = setup(repo, tmp_path, monkeypatch, checks=(0, red))
    assert run(repo, v) == 1
    assert capsys.readouterr().out.strip() == "ERROR checks failing: ci, lint"
    assert not merged(log)


def test_no_checks_reported_blocks_a_direct_merge(repo, tmp_path, monkeypatch, capsys):
    _, log, v = setup(repo, tmp_path, monkeypatch, checks=(1, "no checks reported on the 'feature/x' branch"))
    assert run(repo, v) == 1
    assert capsys.readouterr().out.startswith("ERROR no checks reported")
    assert not merged(log)


def test_only_the_scripts_own_status_counts_as_no_checks(repo, tmp_path, monkeypatch, capsys):
    own = json.dumps([{"name": "verify/user-flow", "bucket": "pass"}])
    _, log, v = setup(repo, tmp_path, monkeypatch, checks=(0, own))
    assert run(repo, v) == 1
    assert capsys.readouterr().out.startswith("ERROR no checks reported")
    assert not merged(log)


def test_unreadable_checks_block_a_direct_merge(repo, tmp_path, monkeypatch, capsys):
    _, log, v = setup(repo, tmp_path, monkeypatch, checks=(1, "HTTP 502"))
    assert run(repo, v) == 1
    assert "ERROR gh pr checks: HTTP 502" in capsys.readouterr().out
    assert not merged(log)


PENDING = json.dumps([{"name": "ci", "bucket": "pending"}, {"name": "verify/user-flow", "bucket": "pass"}])


def test_pending_checks_are_polled_until_green(repo, tmp_path, monkeypatch, capsys):
    sha, log, v = setup(repo, tmp_path, monkeypatch, checks=(0, PENDING))
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            rules = json.loads(os.environ["FAKE_GH_RULES"])
            monkeypatch.setenv("FAKE_GH_RULES", json.dumps([[["pr", "checks"], 0, GREEN], *rules]))

    monkeypatch.setattr(automerge.time, "sleep", sleep)
    assert run(repo, v) == 0
    assert slept == [20, 20]
    assert capsys.readouterr().out.strip() == f"MERGED PR #5 at {sha[:7]}"
    assert len(merged(log)) == 1


def test_pending_checks_give_up_after_the_wait(repo, tmp_path, monkeypatch, capsys):
    _, log, v = setup(repo, tmp_path, monkeypatch, checks=(0, PENDING))
    slept = []
    monkeypatch.setattr(automerge.time, "sleep", slept.append)
    assert run(repo, v, "--wait", "50") == 1
    assert slept == [20, 20, 10]
    assert capsys.readouterr().out.startswith("PENDING checks still running")
    assert not merged(log)
    assert run(repo, v, "--wait", "0") == 1
    assert slept == [20, 20, 10]


def test_failing_rulesets_call_counts_as_no_ruleset(repo, tmp_path, monkeypatch, capsys):
    sha, log, v = setup(repo, tmp_path, monkeypatch, rulesets=(1, "HTTP 403"))
    assert run(repo, v) == 0
    assert capsys.readouterr().out.strip() == f"MERGED PR #5 at {sha[:7]}"
    assert ["pr", "merge", "5", "--squash", "--delete-branch", "--match-head-commit", sha] in calls(log)


HELD = {".github/workflows/x.yml": "on: push\n"}


def test_hold_issue_falls_back_to_no_label(repo, tmp_path, monkeypatch, capsys):
    first = [[["issue", "create"], 1, "could not add label: 'ready-for-human' not found", ["--label"]]]
    _, log, v = setup(repo, tmp_path, monkeypatch, files=HELD, first=first)
    assert run(repo, v) == 3
    created = [x for x in calls(log) if x[:2] == ["issue", "create"]]
    assert len(created) == 2 and "--label" in created[0] and "--label" not in created[1]
    assert created[1][created[1].index("--title") + 1] == "Held: PR #5 T"
    out = capsys.readouterr().out
    assert out.startswith("HOLD sensitive paths: .github/workflows/x.yml") and "ERROR" not in out
    assert "NOTE" in out and "not found" in out


def test_hold_survives_a_failed_issue_and_reports_the_error(repo, tmp_path, monkeypatch, capsys):
    first = [[["issue", "create"], 1, "API rate limit exceeded"]]
    _, log, v = setup(repo, tmp_path, monkeypatch, files=HELD, first=first)
    assert run(repo, v) == 3
    out = capsys.readouterr().out
    assert out.startswith("HOLD sensitive paths: .github/workflows/x.yml")
    assert "ERROR" in out and "API rate limit exceeded" in out
    assert not merged(log)


def test_approve_stops_when_the_label_cannot_be_added(repo, tmp_path, monkeypatch, capsys):
    first = [[["pr", "edit"], 1, "'human-approved' not found"]]
    _, log, v = setup(repo, tmp_path, monkeypatch, files=HELD, first=first)
    assert run(repo, v, "--approve") == 1
    out = capsys.readouterr().out
    assert out.startswith("ERROR") and "human-approved" in out
    assert not merged(log)


def test_pending_exit_code_of_gh_is_still_read_as_pending(repo, tmp_path, monkeypatch, capsys):
    _, log, v = setup(repo, tmp_path, monkeypatch, checks=(8, PENDING))
    assert run(repo, v, "--wait", "0") == 1
    assert capsys.readouterr().out.strip() == "PENDING checks still running: ci"
    assert not merged(log)


AUTO_MERGE = ["pr", "merge", "5", "--squash", "--delete-branch", "--match-head-commit"]
STATE = ["pr", "view", "5", "--json", "state", "-q", ".state"]
RED = json.dumps([{"name": "ci", "bucket": "fail"}, {"name": "verify/user-flow", "bucket": "pass"}])
OWN = json.dumps([{"name": "verify/user-flow", "bucket": "pass"}])


def test_ruleset_arms_only_after_the_scripts_own_check(repo, tmp_path, monkeypatch, capsys):
    sha, log, v = setup(repo, tmp_path, monkeypatch, rulesets=(0, "2"), state=(0, "OPEN"))
    assert run(repo, v) == 0
    assert capsys.readouterr().out.strip() == f"ARMED PR #5 at {sha[:7]}"
    c = calls(log)
    assert ["api", "repos/me/r/rulesets", "--jq", "length"] in c
    kinds = [x[:2] for x in c]
    assert kinds.index(["api", f"repos/me/r/statuses/{sha}"]) < kinds.index(["pr", "checks"]) \
        < c.index([*AUTO_MERGE, sha, "--auto"]) < c.index(STATE)


@pytest.mark.parametrize("extra, rulesets", [((), (0, "2")), (("--auto",), (0, "0"))])
@pytest.mark.parametrize("checks, said", [(RED, "ERROR checks failing: ci"),
                                          (PENDING, "PENDING checks still running: ci"),
                                          (OWN, "ERROR no checks reported")])
def test_red_running_or_missing_ci_is_never_armed(repo, tmp_path, monkeypatch, capsys, extra, rulesets, checks,
                                                  said):
    _, log, v = setup(repo, tmp_path, monkeypatch, rulesets=rulesets, checks=(0, checks))
    assert run(repo, v, "--wait", "0", *extra) == 1
    assert capsys.readouterr().out.startswith(said)
    assert not merged(log)


def test_auto_merge_that_went_through_at_once_prints_merged(repo, tmp_path, monkeypatch, capsys):
    sha, log, v = setup(repo, tmp_path, monkeypatch, rulesets=(0, "2"), state=(0, "MERGED"))
    assert run(repo, v) == 0
    assert capsys.readouterr().out.strip() == f"MERGED PR #5 at {sha[:7]}"
    assert [*AUTO_MERGE, sha, "--auto"] in calls(log)


@pytest.mark.parametrize("state", [(0, "OPEN"), (1, "HTTP 502")])
def test_a_merge_that_is_not_confirmed_prints_armed(repo, tmp_path, monkeypatch, capsys, state):
    sha, log, v = setup(repo, tmp_path, monkeypatch, state=state)
    assert run(repo, v) == 0
    assert capsys.readouterr().out.strip() == f"ARMED PR #5 at {sha[:7]}"
    assert [*AUTO_MERGE, sha] in calls(log)


def test_auto_flag_skips_only_the_ruleset_lookup(repo, tmp_path, monkeypatch, capsys):
    sha, log, v = setup(repo, tmp_path, monkeypatch, state=(0, "OPEN"))
    assert run(repo, v, "--auto") == 0
    assert capsys.readouterr().out.strip() == f"ARMED PR #5 at {sha[:7]}"
    c = calls(log)
    assert not any(x[:2] == ["api", "repos/me/r/rulesets"] for x in c)
    assert any(x[:2] == ["pr", "checks"] for x in c) and [*AUTO_MERGE, sha, "--auto"] in c
