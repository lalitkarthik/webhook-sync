"""Validation of the verifier's verdict.json, shared by evidence_check.py and automerge.py.

As a command, the orchestrator's own check of a ticket verdict (the verifier's Stop hook gives up after
max_stop_blocks, so the hook alone is not enough):
    python scripts/claude/verdict.py --dir <evidence dir> --sha <head sha> --acs AC-3-1,AC-3-2
Prints PASS (exit 0); FAIL and one line per entry that did not pass (exit 1); or INVALID and the
problems when the verdict is missing, malformed, not pinned to the sha or not backed by its evidence
(exit 1).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fl_lib as fl  # noqa: E402

STATUSES = {"PASS", "FAIL", "FLAKY", "NO_TEST"}


def validate(v, head_sha: str | None, evidence_root, expected_ids=None) -> list[str]:
    """The reasons a verdict is not accepted; none means it is.

    With expected_ids, every one of those AC IDs must be listed. A badly shaped verdict is reported
    as a problem, never raised, so the callers can fail closed.
    """
    if not isinstance(v, dict):
        return ["verdict.json must hold one JSON object"]
    problems: list[str] = []
    root = Path(evidence_root)
    if head_sha and v.get("sha") != head_sha:
        problems.append(f"verdict is for {str(v.get('sha'))[:7]}, head is {head_sha[:7]}")
    acs = v.get("ac") or []
    if not isinstance(acs, list):
        problems.append("'ac' must be a list of objects")
        acs = []
    elif not acs:
        problems.append("verdict lists no acceptance criteria")
    seen: set[str] = set()
    all_pass = bool(acs)
    for n, ac in enumerate(acs):
        if not isinstance(ac, dict):
            problems.append(f"ac[{n}]: must be an object with id, status, evidence, observed and repro")
            all_pass = False
            continue
        i, st = ac.get("id"), ac.get("status")
        if not isinstance(i, str) or not i.strip():
            i = f"ac[{n}]"
            problems.append(f"{i}: id must be a non-empty string")
        elif i in seen:
            problems.append(f"{i}: listed more than once")
        seen.add(i)
        all_pass = all_pass and st == "PASS"
        if not isinstance(st, str) or st not in STATUSES:
            problems.append(f"{i}: status must be one of {', '.join(sorted(STATUSES))}")
            continue
        if st == "PASS":
            evidence = ac.get("evidence") or []
            if not isinstance(evidence, list) or not all(isinstance(e, str) and e for e in evidence):
                problems.append(f"{i}: evidence must be a list of file paths")
                continue
            files = [Path(e) if Path(e).is_absolute() else root / e for e in evidence]
            if not files:
                problems.append(f"{i}: PASS without evidence")
                continue
            missing = [str(f) for f in files if not f.is_file() or f.stat().st_size == 0]
            if missing:
                problems.append(f"{i}: evidence missing or empty: {', '.join(missing)}")
                continue
            texts = [f.read_text(encoding="utf-8", errors="replace") for f in files
                     if f.suffix.lower() in fl.TEXT_SUFFIXES]
            observed = ac.get("observed")
            observed = observed.strip() if isinstance(observed, str) else ""
            if not texts:
                problems.append(f"{i}: PASS needs at least one text evidence file (snapshot, output, response, log)")
            elif not observed or not any(observed in t for t in texts):
                problems.append(f"{i}: 'observed' must quote text that appears in its text evidence")
        elif st in ("FAIL", "FLAKY") and not (isinstance(ac.get("repro"), str) and ac["repro"].strip()):
            problems.append(f"{i}: {st} without repro steps")
    left_out = sorted(set(expected_ids or []) - seen)
    if left_out:
        problems.append("verdict leaves out: " + ", ".join(left_out))
    expected = "PASS" if all_pass else "FAIL"
    if v.get("overall") != expected:
        problems.append(f"overall must be {expected}")
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dir", required=True, help="the evidence folder holding verdict.json")
    ap.add_argument("--sha", required=True, help="the head sha the verdict must be pinned to")
    ap.add_argument("--acs", default="", help="AC IDs the verdict must list, comma-separated")
    a = ap.parse_args(argv)
    folder = Path(a.dir)
    try:
        v = json.loads((folder / "verdict.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"INVALID verdict.json unreadable in {folder.as_posix()}: {e}")
        return 1
    problems = validate(v, a.sha, folder, [x.strip() for x in a.acs.split(",") if x.strip()])
    if problems:
        print("INVALID " + "; ".join(problems))
        return 1
    if v["overall"] == "PASS":
        print("PASS")
        return 0
    print("FAIL")
    for ac in v["ac"]:
        if ac["status"] != "PASS":
            evidence = ", ".join(ac.get("evidence") or [])
            print(f"{ac['id']} {ac['status']}: {ac.get('repro') or ''} (evidence: {evidence})")
    return 1


if __name__ == "__main__":
    sys.exit(main())
