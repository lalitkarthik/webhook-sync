"""Checks that every acceptance criterion of an issue has a tagged test.

    python scripts/claude/ac_check.py --issue 142 --expect red
With --expect, each AC's test runs on its own and must fail (red) or pass (green). A run in which
no test ran (NOTEST) is neither: nothing selected, a collection error, a usage error.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fl_lib as fl  # noqa: E402

NOTEST_RE = re.compile(r"no tests ran|\b0 selected\b|no test files found|no tests found", re.I)


def tagged_ids(root: Path, cfg: dict) -> set[str]:
    found: set[str] = set()
    for d in cfg["ac_dirs"]:
        base = root / d
        if not base.exists():
            continue
        for p in base.rglob("*"):
            found |= fl.ac_ids(p.relative_to(root).as_posix())
            if p.is_file() and p.stat().st_size < 2_000_000:
                found |= fl.ac_ids(p.read_text(encoding="utf-8", errors="replace"))
    return found


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--issue")
    src.add_argument("--body-file")
    ap.add_argument("--expect", choices=["red", "green"])
    ap.add_argument("--root", default=".")
    a = ap.parse_args(argv)

    root = fl.repo_root(a.root)
    cfg = fl.load_config(root)
    if a.issue:
        code, body = fl.gh(["issue", "view", a.issue, "--json", "body", "-q", ".body"], root)
        if code:
            print(f"ERROR gh issue view: {body}")
            return 1
    else:
        body = Path(a.body_file).read_text(encoding="utf-8")
    ids = sorted(fl.ac_ids(body))
    if not ids:
        print("ERROR the issue lists no AC IDs (AC-<issue>-<n>)")
        return 1

    ok = True
    for i in sorted(set(ids) - tagged_ids(root, cfg)):
        print(f"MISSING {i}")
        ok = False
    if a.expect and ok:
        if not cfg["ac_cmd"]:
            print("ERROR ac_cmd is not configured in scripts/claude/fl-config.json")
            return 1
        for i in ids:
            code, out = fl.run_cmd(fl.format_cmd(cfg["ac_cmd"], ids=[i]), root)
            if code in cfg["ac_notest_codes"] or NOTEST_RE.search(out):
                print(f"NOTEST {i}: no test ran (exit {code})\n{out}")
                ok = False
                continue
            got = "green" if code == 0 else "red"
            if got == a.expect:
                print(f"{got.upper()} {i}")
            else:
                print(f"WRONG {i}: expected {a.expect}, got {got}\n{out}")
                ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
