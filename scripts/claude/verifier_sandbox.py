"""PreToolUse hook (Bash) for fl-verifier: the verifier may run and observe, not change things."""
from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fl_lib as fl  # noqa: E402

DENY = [
    (re.compile(r"\bgit\s+(?:commit|push|reset|checkout|switch|merge|rebase|stash|clean|add|rm|mv|restore|"
                r"cherry-pick|revert|tag|apply|am)\b"), "git write"),
    (re.compile(r"\bgit\s+branch\s+-[dDmMcC]"), "git branch change"),
    (re.compile(r"\bsed\s+(?:-[a-zA-Z]*i|--in-place)"), "in-place edit"),
    (re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:install|i|add|remove|uninstall|update|upgrade)\b"), "package change"),
    (re.compile(r"\b(?:pip|uv)\s+(?:install|add|remove|sync|lock)\b|\buv\s+pip\s+install\b"), "package change"),
    (re.compile(r"\bgh\s+(?:pr|issue)\s+(?:create|merge|close|edit|comment|review|reopen|ready)\b"), "GitHub write"),
    (re.compile(r"\bgh\s+api\b.*(?:\s-X\s*(?:POST|PUT|PATCH|DELETE)|\s--method\s+(?:POST|PUT|PATCH|DELETE)|"
                r"\s-[fF]\s|\s--field\s|\s--raw-field\s|\s--input\s)"), "GitHub write"),
]
REDIRECT = re.compile(r"(?<![<&])\d?>{1,2}\s*([^\s;&|<>]+)")
FILE_CMD = re.compile(r"(?:^|[;&|(]\s*|\s)(rm|mv|cp|touch|mkdir|tee|truncate|chmod|ln)\s+([^;&|]*)")
ALLOWED_DIR = re.compile(r"(?:^|/)(?:verification|\.verify)(?:/|$)")


def _ok_target(t: str) -> bool:
    t = t.strip("'\"").replace("\\", "/")
    return (t in ("/dev/null", "NUL") or t.startswith(("$EVID", "${EVID}"))
            or bool(ALLOWED_DIR.search(t)))


def _split(s: str) -> list[str]:
    try:
        return shlex.split(s)
    except ValueError:
        return s.split()


def decide(command: str) -> str | None:
    for rx, why in DENY:
        if rx.search(command):
            return f"{why} is not allowed for the verifier: {command[:120]}"
    for m in REDIRECT.finditer(command):
        if not _ok_target(m.group(1)):
            return f"the verifier may only write under verification/ or .verify/, not {m.group(1)}"
    for m in FILE_CMD.finditer(command):
        args = [x for x in _split(m.group(2)) if not x.startswith("-") and not x.startswith("<")]
        targets = args[-1:] if m.group(1) in ("cp", "mv", "ln") else args
        bad = [t for t in targets if not _ok_target(t)]
        if bad:
            return f"{m.group(1)} outside verification/ or .verify/: {' '.join(bad)}"
    return None


def check(data: dict) -> str | None:
    reason = decide((data.get("tool_input") or {}).get("command", ""))
    return reason + ". Record what you observed in verdict.json instead." if reason else None


def main() -> int:
    return fl.run_pretool_hook("verifier_sandbox", check)


if __name__ == "__main__":
    sys.exit(main())
