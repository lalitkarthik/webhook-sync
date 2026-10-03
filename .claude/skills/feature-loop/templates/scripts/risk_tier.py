"""Decides whether a PR may auto-merge (`auto`) or waits for the user (`hold`).

    python scripts/claude/risk_tier.py --base origin/main
Exit 0 for auto, 3 for hold. Rules come from scripts/claude/fl-config.json (risk_hold_globs,
rule_config_globs, protected_globs, pr_max_lines, pr_max_files).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fl_checks  # noqa: E402
import fl_lib as fl  # noqa: E402

# An added rule-config line with one of these may relax a rule (`ignore` also covers per-file-ignores).
# `allow` not inside `disallow`, and `warn`/`warning` only as a value, not inside an option name: adding
# `disallow_untyped_defs = true` or `warn_return_any = true` adds a rule (spec Q23).
RELAX_RE = re.compile(r"ignore|exclude|\boff\b|\bfalse\b|disable|(?<!dis)allow|(?<![\w-])warn(?:ing)?(?![\w-])|"
                      r"select[\"']?\s*[:=]\s*\[\s*\]", re.I)


def assess(base: str, root) -> list[str]:
    root = Path(root)
    cfg = fl.load_config(root)
    mb = fl.merge_base(base, root)
    ns = fl.name_status(mb, root)
    stats = fl.numstat(mb, root)
    added = fl.added_lines(mb, root)
    paths = sorted({p for _, p, _ in ns} | {o for _, _, o in ns if o})
    reasons: list[str] = []
    hits = [p for p in paths if fl.matches(p, cfg["risk_hold_globs"])]
    if hits:
        reasons.append("sensitive paths: " + ", ".join(hits[:10]))
    locks = [p for p in paths if fl.matches(p, fl.LOCK_GLOBS)]
    if locks:
        reasons.append("dependency change: " + ", ".join(locks))
    reasons += fl_checks.test_problems(mb, root, cfg, ns, added)
    reasons += [r for r in fl_checks.protected_problems(mb, root, cfg, ns, [])
                if not any(r.endswith(lock) for lock in locks)]
    size = fl_checks.size_problem(cfg, stats, prefix="pr_")
    if size:
        reasons.append(size)
    relaxed = sorted(p for _a, d, p in stats if d > 0 and fl.matches(p, cfg["rule_config_globs"]))
    if relaxed:
        reasons.append("rule config lines removed (a rule may be relaxed): " + ", ".join(relaxed))
    relaxing = sorted(f"{p}: {line.strip()[:80]}" for p, lines in added.items()
                      if fl.matches(p, cfg["rule_config_globs"]) for line in lines if RELAX_RE.search(line))
    if relaxing:
        reasons.append("rule config may be relaxed by added lines: " + ", ".join(relaxing[:10]))
    nested = sorted(p for st, p, _ in ns if st != "D" and "/" in p and fl.matches(p, cfg["rule_config_globs"])
                    and not fl.exists_at(mb, p, root))
    if nested:
        reasons.append("new nested rule config (it overrides a stricter parent): " + ", ".join(nested))
    return reasons


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--root", default=".")
    a = ap.parse_args(argv)
    reasons = assess(a.base, fl.repo_root(a.root))
    if not reasons:
        print("auto")
        return 0
    print("hold")
    for r in reasons:
        print(f"- {r}")
    return 3


if __name__ == "__main__":
    sys.exit(main())
