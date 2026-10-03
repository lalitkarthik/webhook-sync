"""SubagentStop hook for fl-verifier: no finishing without a valid verdict.json for HEAD.

The verdict must cover every AC of the ticket. A crash of the hook itself blocks too, and counts
toward max_stop_blocks like any other block.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fl_lib as fl  # noqa: E402
import verdict  # noqa: E402


def check(data: dict, root: Path) -> str | None:
    ticket = fl.load_ticket(root)
    ev = ticket.get("evidence_dir")
    if not ev:
        return None
    ev = Path(ev)
    vf = ev / "verdict.json"
    if not vf.is_file():
        problems = [f"no verdict.json in {ev.as_posix()}"]
    else:
        try:
            v = json.loads(vf.read_text(encoding="utf-8"))
        except ValueError as e:
            problems = [f"verdict.json is not valid JSON: {e}"]
        else:
            problems = verdict.validate(v, fl.git(["rev-parse", "HEAD"], root).strip(), ev,
                                        ticket.get("acs") or None)
    if not problems:
        return None
    return "Verdict not accepted:\n- " + "\n- ".join(problems) + "\nFix verdict.json, then finish."


def main() -> int:
    return fl.run_stop_hook("verdict", check)


if __name__ == "__main__":
    sys.exit(main())
