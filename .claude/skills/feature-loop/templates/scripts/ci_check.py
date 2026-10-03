"""Runs the repo's checks from scripts/claude/fl-config.json.

    python scripts/claude/ci_check.py                        # typecheck, lint, suite, every tagged AC, boundaries
    python scripts/claude/ci_check.py --acs AC-3-1,AC-3-2    # only these ACs ("" for none)
    python scripts/claude/ci_check.py --only smoke
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ac_check  # noqa: E402
import fl_lib as fl  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--acs")
    ap.add_argument("--only", choices=["smoke"])
    ap.add_argument("--root", default=".")
    a = ap.parse_args(argv)
    root = fl.repo_root(a.root)
    cfg = fl.load_config(root)
    if a.only == "smoke":
        cmds = [("smoke", cfg["smoke_cmd"])]
    else:
        ids = (sorted(x.strip() for x in a.acs.split(",") if x.strip()) if a.acs is not None
               else sorted(ac_check.tagged_ids(root, cfg)))
        cmds = [("typecheck", cfg["typecheck"]), ("lint", cfg["lint"]), ("full suite", cfg["full_suite"]),
                ("acceptance", fl.format_cmd(cfg["ac_cmd"], ids=ids) if ids and cfg["ac_cmd"] else ""),
                ("boundaries", cfg["boundaries_cmd"])]
    failed = False
    for name, cmd in cmds:
        if not cmd:
            print(f"SKIP {name}")
            continue
        code, out = fl.run_cmd(cmd, root)
        print(f"PASS {name}" if code == 0 else f"FAIL {name}: {out}")
        failed |= code != 0
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
