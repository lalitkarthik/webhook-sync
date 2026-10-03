"""The ticket gate (L3): zero-token checks the orchestrator runs on a finished ticket.

Run in the ticket worktree after merging the feature branch into it:
    python scripts/claude/ticket_gate.py --base feature/<slug> [--acs AC-3-1,AC-3-2]
The orchestrator passes its own --ticket and --config, so nothing a worker leaves in the worktree
decides what the gate runs; a worktree with uncommitted or untracked files fails the gate.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fl_checks  # noqa: E402
import fl_lib as fl  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True)
    ap.add_argument("--acs", default="", help="extra AC IDs to re-run, comma-separated")
    ap.add_argument("--ticket", help="ticket JSON; default <root>/.fl-ticket.json")
    ap.add_argument("--config", help="fl-config.json; default <root>/scripts/claude/fl-config.json")
    ap.add_argument("--root", default=".")
    a = ap.parse_args(argv)

    root = fl.repo_root(a.root)
    cfg = fl.load_config(root, a.config)
    ticket = json.loads(Path(a.ticket).read_text(encoding="utf-8")) if a.ticket else fl.load_ticket(root)
    mb = fl.merge_base(a.base, root)
    ns = fl.name_status(mb, root)
    allow = ticket.get("allow", [])
    results: list[tuple[str, str, str]] = []

    # Only commits are merged, and the checks below diff only commits (ignored files do not count).
    dirty = sorted({ln[3:] for ln in fl.git(["status", "--porcelain", "--untracked-files=all"], root).splitlines()
                    if ln.strip()})
    results.append(("FAIL" if dirty else "PASS", "clean",
                    "uncommitted or untracked: " + ", ".join(dirty) if dirty else ""))

    areas = ticket.get("areas") or []
    if areas:
        ok = [ar.rstrip("/") + "/**" for ar in areas] + list(areas) + cfg["always_allowed"] + allow
        outside = sorted({q for _st, p, old in ns for q in (p, old) if q and not fl.matches(q, ok)})
        results.append(("FAIL" if outside else "PASS", "areas", ", ".join(outside)))
    else:
        results.append(("SKIP", "areas", "the ticket declares no areas"))

    tp = fl_checks.test_problems(mb, root, cfg, ns, fl.added_lines(mb, root))
    results.append(("FAIL" if tp else "PASS", "tests", "; ".join(tp)))
    pp = fl_checks.protected_problems(mb, root, cfg, ns, allow)
    results.append(("FAIL" if pp else "PASS", "protected", "; ".join(pp)))
    sp = fl_checks.size_problem(cfg, fl.numstat(mb, root))
    results.append(("FAIL" if sp else "PASS", "size", sp or ""))

    static_ok = all(r[0] != "FAIL" for r in results)
    acs = sorted(set(ticket.get("acs") or []) | {x.strip() for x in a.acs.split(",") if x.strip()})
    sources = sorted(p for st, p, _ in ns if st != "D" and Path(p).suffix in cfg["source_suffixes"]
                     and not fl_checks.is_test(p, cfg))
    commands = [
        ("typecheck", cfg["typecheck"]),
        ("lint", cfg["lint"]),
        ("full suite", cfg["full_suite"]),
        ("acceptance", fl.format_cmd(cfg["ac_cmd"], ids=acs) if acs and cfg["ac_cmd"] else ""),
        ("boundaries", cfg["boundaries_cmd"]),
        ("mutation", fl.format_cmd(cfg["mutation_cmd"], files=sources) if sources and cfg["mutation_cmd"] else ""),
    ]
    for name, cmd in commands:
        if name == "acceptance" and acs and not cfg["ac_cmd"]:
            results.append(("FAIL", name, "ac_cmd not configured"))
        elif not static_ok:
            results.append(("SKIP", name, "a static check failed first"))
        elif not cmd:
            results.append(("SKIP", name, "not configured or nothing to run"))
        else:
            code, out = fl.run_cmd(cmd, root)
            results.append(("PASS" if code == 0 else "FAIL", name, "" if code == 0 else out))

    for st, name, detail in results:
        print(f"{st} {name}" + (f": {detail}" if detail else ""))
    return 1 if any(r[0] == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
