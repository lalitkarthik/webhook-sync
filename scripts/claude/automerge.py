"""The only way a PR reaches main.

    python scripts/claude/automerge.py --pr 12 --verdict verification/feature/<sha7>/verdict.json
    python scripts/claude/automerge.py --pr 12 --verdict <path> --approve     # the user approved a held PR
Run from a checkout whose HEAD is the PR head.

The script requires every check on the PR to be green itself, waiting up to --wait seconds for
pending ones. Only then does it merge, with GitHub auto-merge (`--auto`) when the repo has a ruleset
or --auto is given. It then asks GitHub for the PR's state and prints MERGED when the PR is merged,
ARMED when GitHub will merge it later.
Exit 0 merged or armed, 1 error or checks not green, 3 held for the user, 4 auto-merge switched off.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fl_lib as fl  # noqa: E402
import risk_tier  # noqa: E402
import verdict  # noqa: E402

VERIFY_CONTEXT = "verify/user-flow"
POLL_SECONDS = 20


def open_hold_issue(title: str, body: str, root) -> str | None:
    """Opens the ready-for-human issue, without the label when the label cannot be set.

    Returns the line to print when that did not go as intended.
    """
    args = ["issue", "create", "--title", title, "--body", body]
    code, out = fl.gh(args + ["--label", "ready-for-human"], root)
    if code == 0:
        return None
    code, again = fl.gh(args, root)
    if code == 0:
        return f"NOTE the issue was opened without the ready-for-human label: {out}"
    return f"ERROR the ready-for-human issue was not opened: {again}"


def has_ruleset(repo: str, root) -> bool:
    code, out = fl.gh(["api", f"repos/{repo}/rulesets", "--jq", "length"], root)
    return code == 0 and out.strip().isdigit() and int(out) > 0


def checks_state(pr: str, root) -> tuple[str, str]:
    """`pass`, `pending`, `fail`, `none` or `error` for the PR's checks, with the names or the gh error.

    The script's own verify/user-flow status is checked like any other, but alone it is not CI: a PR
    that reports nothing else is `none`.
    """
    code, out = fl.gh(["pr", "checks", pr, "--json", "name,bucket"], root)
    try:
        checks = [(str(c["name"]), c["bucket"]) for c in json.loads(out)]
    except (ValueError, TypeError, KeyError):
        return ("none", "") if code and "no checks reported" in out else ("error", out)
    failing = sorted({n for n, b in checks if b not in ("pass", "skipping", "pending")})
    if failing:
        return "fail", ", ".join(failing)
    if not [n for n, _ in checks if n != VERIFY_CONTEXT]:
        return "none", f"besides {VERIFY_CONTEXT}" if checks else ""
    pending = sorted({n for n, b in checks if b == "pending"})
    return ("pending", ", ".join(pending)) if pending else ("pass", "")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pr", required=True)
    ap.add_argument("--verdict", required=True)
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--auto", action="store_true", help="merge with GitHub auto-merge without looking for a ruleset")
    ap.add_argument("--wait", type=int, default=300, help="seconds to wait for pending checks")
    ap.add_argument("--root", default=".")
    a = ap.parse_args(argv)
    root = fl.repo_root(a.root)

    code, val = fl.gh(["variable", "get", "AUTOMERGE"], root)
    if code == 0 and val.strip().lower() == "off":
        print("OFF auto-merge is switched off (repo variable AUTOMERGE=off); nothing merged")
        return 4

    code, out = fl.gh(["pr", "view", a.pr, "--json", "headRefOid,baseRefName,title"], root)
    if code:
        print(f"ERROR gh pr view: {out}")
        return 1
    pr = json.loads(out)
    head = pr["headRefOid"]
    if fl.git(["rev-parse", "HEAD"], root).strip() != head:
        print(f"ERROR this checkout is not at the PR head {head[:7]}; fetch and check it out first")
        return 1

    if not a.approve:
        reasons = risk_tier.assess(f"origin/{pr['baseRefName']}", root)
        if reasons:
            body = (f"Auto-merge held PR #{a.pr} ({pr['title']}).\n\nWhy:\n"
                    + "\n".join(f"- {r}" for r in reasons)
                    + "\n\nTo merge it anyway, run in the repo, at the PR head:\n\n"
                    + f"    python scripts/claude/automerge.py --pr {a.pr} --verdict {a.verdict} --approve\n\n"
                    + "It adds the human-approved label, which makes CI run risk-gate again. If it "
                    + "prints PENDING, or ERROR naming risk-gate, that run has not finished: run the "
                    + "same command again in a few minutes.\n\n"
                    + "Or close the PR to drop it.")
            trouble = open_hold_issue(f"Held: PR #{a.pr} {pr['title']}", body, root)
            print("HOLD " + "; ".join(reasons))
            if trouble:
                print(trouble)
            return 3

    if a.approve:
        code, out = fl.gh(["pr", "edit", a.pr, "--add-label", "human-approved"], root)
        if code:
            print(f"ERROR adding the human-approved label: {out}")
            return 1

    vpath = Path(a.verdict)
    try:
        v = json.loads(vpath.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"ERROR verdict unreadable: {e}")
        return 1
    problems = verdict.validate(v, head, vpath.parent)
    if problems:
        print("ERROR verdict not accepted: " + "; ".join(problems))
        return 1

    code, repo = fl.gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], root)
    if code:
        print(f"ERROR gh repo view: {repo}")
        return 1
    passed = sum(1 for x in v["ac"] if x.get("status") == "PASS")
    code, out = fl.gh(["api", f"repos/{repo}/statuses/{head}", "-f", "state=success", "-f",
                       f"context={VERIFY_CONTEXT}", "-f", f"description={passed}/{len(v['ac'])} AC PASS"], root)
    if code:
        print(f"ERROR posting the verify status: {out}")
        return 1

    # The script checks CI itself on both merge paths: a ruleset may not require these checks, and
    # `gh pr merge --auto` merges at once when nothing it requires is pending.
    waited = 0
    while True:
        state, detail = checks_state(a.pr, root)
        if state != "pending":
            break
        if waited >= a.wait:
            print(f"PENDING checks still running: {detail}")
            return 1
        step = min(POLL_SECONDS, a.wait - waited)
        time.sleep(step)
        waited += step
    if state != "pass":
        print({"fail": f"ERROR checks failing: {detail}", "none": f"ERROR no checks reported {detail}".rstrip(),
               "error": f"ERROR gh pr checks: {detail}"}[state])
        return 1
    merge = ["pr", "merge", a.pr, "--squash", "--delete-branch", "--match-head-commit", head]
    code, out = fl.gh(merge + (["--auto"] if a.auto or has_ruleset(repo, root) else []), root)
    if code:
        print(f"ERROR gh pr merge: {out}")
        return 1
    code, out = fl.gh(["pr", "view", a.pr, "--json", "state", "-q", ".state"], root)
    done = code == 0 and out.strip() == "MERGED"
    print(f"{'MERGED' if done else 'ARMED'} PR #{a.pr} at {head[:7]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
