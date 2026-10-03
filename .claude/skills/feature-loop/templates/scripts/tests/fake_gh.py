# tests/fake_gh.py
"""Fake gh: logs argv to FAKE_GH_LOG, answers from FAKE_GH_RULES ([[prefix list, exit code, stdout], ...]).

A rule may carry a fourth item: a list of arguments that must all appear somewhere in argv.
"""
import json
import os
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")
for prefix, code, out, *has in json.loads(os.environ.get("FAKE_GH_RULES", "[]")):
    if args[:len(prefix)] == prefix and all(x in args for x in (has[0] if has else [])):
        print(out)
        sys.exit(code)
sys.exit(0)
