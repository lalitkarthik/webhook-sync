"""Shared helpers for the feature-loop gate scripts. Standard library only."""
from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

AC_RE = re.compile(r"AC[-_](\d+)[-_](\d+)")
TEST_DEF_RE = re.compile(
    r"^[ \t]*(?:async[ \t]+)?def[ \t]+test_\w*|^[ \t]*(?:it|test)(?:\.each\([^\n]*?\))?[ \t]*\(",
    re.M,
)
SKIP_RE = re.compile(
    r"\.skip\(|\.only\(|\bfixme\(|pytest\.mark\.(?:skip|xfail)|\bxfail\(|@ts-ignore|@ts-nocheck|"
    r"eslint-disable|#\s*type:\s*ignore|#\s*pyright:\s*ignore|#\s*noqa|\bretries\s*:\s*[1-9]"
)
LOCKFILES = ["uv.lock", "poetry.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb"]
LOCK_GLOBS = [f"**/{name}" for name in LOCKFILES]
# Test-runner and coverage config a worker may neither edit nor create (spec L2).
TEST_CONFIG_GLOBS = ["**/pytest.ini", "**/playwright.config.*", "**/vitest.config.*", "**/jest.config.*",
                     "**/.coveragerc"]
TEXT_SUFFIXES = {".txt", ".json", ".yml", ".yaml", ".md", ".log", ".html", ".csv", ".out", ".xml"}

DEFAULTS: dict = {
    "typecheck": "",
    "lint": "",
    "full_suite": "",
    "ac_cmd": "",
    "mutation_cmd": "",
    "boundaries_cmd": "",
    "smoke_cmd": "",
    "max_lines": 400,
    "max_files": 25,
    "pr_max_lines": 2000,
    "pr_max_files": 60,
    "max_stop_blocks": 3,
    # ac_cmd exit codes that mean "no test ran" (pytest: 2 interrupted or collection error, 3 internal
    # error, 4 usage error, 5 nothing collected; 124 run_cmd timeout, 126 not executable, 127 command
    # not found). A runner that fails an assertion with one of these (Hurl exits 4) needs its own list.
    "ac_notest_codes": [2, 3, 4, 5, 124, 126, 127],
    "ac_dirs": ["tests/acceptance", "tests/golden", "tests/http", "e2e"],
    "test_globs": ["tests/**", "test/**", "e2e/**", "**/*.test.*", "**/*.spec.*", "**/test_*.py", "**/*_test.py"],
    "source_suffixes": [".py", ".ts", ".tsx", ".js", ".jsx", ".mjs"],
    "protected_globs": [
        "tests/acceptance/**", "tests/golden/**", "tests/http/**", "e2e/**",
        "**/__snapshots__/**", "**/*.ambr", "**/*.expected.json",
        "**/playwright.config.*", "**/vitest.config.*", "**/jest.config.*", "**/pytest.ini", "**/stryker.conf.*",
        "scripts/claude/**", ".claude/**", ".fl-ticket.json", *LOCK_GLOBS,
    ],
    "always_protected": ["scripts/claude/**", ".claude/**", ".fl-ticket.json", *TEST_CONFIG_GLOBS],
    "always_allowed": ["tests/**", "CHANGELOG.md", "CONTEXT.md", "docs/**"],
    "size_exempt": [*LOCK_GLOBS, "**/__snapshots__/**", "**/*.ambr", "tests/golden/**"],
    "risk_hold_globs": [
        "**/auth/**", "**/middleware.*", "**/billing/**", "**/*stripe*", "**/*payment*", "**/migrations/**",
        "alembic/versions/**", "prisma/migrations/**", ".github/**", "**/CLAUDE.md", "**/AGENTS.md",
        ".claude/**", "**/.env*", "**/Dockerfile", "**/docker-compose*", "vercel.json", "fly.toml",
        "netlify.toml", "scripts/claude/**", "**/*session*", "**/*jwt*", "**/*signature*", "**/*hmac*",
    ],
    "rule_config_globs": [
        "**/eslint.config.*", "**/.eslintrc*", "**/.dependency-cruiser.*", "**/.importlinter",
        "**/ruff.toml", "**/.ruff.toml", "**/pyrightconfig.json", "**/tsconfig*.json", "**/pyproject.toml",
    ],
}

_GLOB_CACHE: dict[str, re.Pattern] = {}


def glob_re(pattern: str) -> re.Pattern:
    if pattern not in _GLOB_CACHE:
        out, i = [], 0
        while i < len(pattern):
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
            elif pattern.startswith("**", i):
                out.append(".*")
                i += 2
            elif pattern[i] == "*":
                out.append("[^/]*")
                i += 1
            elif pattern[i] == "?":
                out.append("[^/]")
                i += 1
            else:
                out.append(re.escape(pattern[i]))
                i += 1
        _GLOB_CACHE[pattern] = re.compile("^" + "".join(out) + "$")
    return _GLOB_CACHE[pattern]


def matches(path: str, globs) -> bool:
    p = path.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return any(glob_re(g).match(p) for g in globs)


def load_config(root, path=None) -> dict:
    """DEFAULTS updated from path, by default <root>/scripts/claude/fl-config.json."""
    cfg = copy.deepcopy(DEFAULTS)
    f = Path(path) if path else Path(root) / "scripts" / "claude" / "fl-config.json"
    if f.is_file():
        cfg.update(json.loads(f.read_text(encoding="utf-8") or "{}"))
    return cfg


def load_ticket(root) -> dict:
    f = Path(root) / ".fl-ticket.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}


def git(args, cwd) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def repo_root(cwd) -> Path:
    return Path(git(["rev-parse", "--show-toplevel"], cwd).strip())


def merge_base(base: str, cwd) -> str:
    return git(["merge-base", base, "HEAD"], cwd).strip()


def exists_at(rev: str, path: str, cwd) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{rev}:{path}"], cwd=str(cwd),
                          capture_output=True).returncode == 0


def file_at(rev: str, path: str, cwd) -> str | None:
    r = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.stdout if r.returncode == 0 else None


def name_status(rev: str, cwd) -> list[tuple[str, str, str | None]]:
    out = []
    for line in git(["diff", "--name-status", "-M", f"{rev}...HEAD"], cwd).splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        st = parts[0][0]
        out.append((st, parts[2], parts[1]) if st in "RC" else (st, parts[1], None))
    return out


def numstat(rev: str, cwd) -> list[tuple[int, int, str]]:
    out = []
    for line in git(["diff", "--numstat", "--no-renames", f"{rev}...HEAD"], cwd).splitlines():
        a, d, p = line.split("\t", 2)
        out.append((int(a) if a != "-" else 0, int(d) if d != "-" else 0, p))
    return out


def added_lines(rev: str, cwd) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur = None
    for line in git(["diff", "-U0", "--no-renames", f"{rev}...HEAD"], cwd).splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else None
        elif cur and line.startswith("+"):
            out.setdefault(cur, []).append(line[1:])
    return out


def count_tests(text: str) -> int:
    return len(TEST_DEF_RE.findall(text or ""))


def ac_ids(text: str) -> set[str]:
    return {f"AC-{a}-{b}" for a, b in AC_RE.findall(text or "")}


def format_cmd(template: str, ids=(), files=()) -> str:
    ids = sorted(ids)
    return (template.replace("{ids_k}", " or ".join(i.replace("-", "_") for i in ids))
            .replace("{ids_pipe}", "|".join(ids))
            .replace("{ids_space}", " ".join(ids))
            .replace("{files}", " ".join(shlex.quote(f) for f in files)))


def bash() -> str:
    return os.environ.get("FL_BASH") or shutil.which("bash") or "bash"


def run_cmd(cmd: str, cwd, timeout: int = 3600) -> tuple[int, str]:
    try:
        r = subprocess.run([bash(), "-c", cmd], cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {cmd}"
    lines = (r.stdout + r.stderr).strip().splitlines()
    return r.returncode, "\n".join(lines[-40:])


def gh(args, cwd=None) -> tuple[int, str]:
    cmd = shlex.split(os.environ.get("FL_GH", "gh")) + [str(a) for a in args]
    r = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout.strip() if r.returncode == 0 else (r.stdout + r.stderr).strip())


def _stop_file(root) -> Path:
    return Path(root) / ".fl-stop-count.json"


def _stop_counts(f: Path) -> dict:
    """The counters in f; a missing or corrupt file counts as none, so the stop hooks cannot crash on it."""
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def bump_stop(root, key: str, session: str) -> int:
    f = _stop_file(root)
    d = _stop_counts(f)
    k = f"{key}:{session}"
    d[k] = (d[k] if isinstance(d.get(k), int) else 0) + 1
    f.write_text(json.dumps(d), encoding="utf-8")
    return d[k]


def reset_stop(root, key: str, session: str) -> None:
    f = _stop_file(root)
    if f.is_file():
        d = _stop_counts(f)
        d.pop(f"{key}:{session}", None)
        f.write_text(json.dumps(d), encoding="utf-8")


def port_for(root) -> int:
    return 3000 + zlib.crc32(str(Path(root).resolve()).lower().encode()) % 1000


def hook_input() -> dict:
    raw = sys.stdin.read()
    return json.loads(raw) if raw.strip() else {}


def hook_block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))


def hook_deny(reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))


def _crash(name: str, e: Exception) -> str:
    return f"{name} could not run its check ({type(e).__name__}: {e})."


def run_pretool_hook(name: str, check) -> int:
    """Runs a PreToolUse hook: check(data) returns the reason to deny the call, or None to allow it.

    A crash denies the call, so a broken hook fails closed. Always returns 0; the hook speaks through JSON.
    """
    try:
        reason = check(hook_input())
    except Exception as e:
        reason = _crash(name, e) + " The call is denied until the check can run."
    if reason:
        hook_deny(reason)
    return 0


def run_stop_hook(key: str, check) -> int:
    """Runs a Stop or SubagentStop hook: check(data, root) returns the reason to block, or None.

    A crash blocks the turn like a red check does. Every block counts toward max_stop_blocks, so neither
    a red gate nor a permanently broken config can keep a turn from ending. Always returns 0.
    """
    root, session = None, ""
    try:
        data = hook_input()
        session = str(data.get("session_id", ""))
        try:
            root = repo_root(data.get("cwd") or ".")
        except RuntimeError:
            return 0
        reason = check(data, root)
    except Exception as e:
        reason = (_crash(f"The {key} hook", e) + " If .fl-ticket.json or scripts/claude/fl-config.json is "
                  "malformed, restore it; otherwise report this in your reply.")
    where = root or tempfile.gettempdir()  # hook input that names no repo is still counted, in the temp dir
    try:
        if not reason:
            reset_stop(where, key, session)
            return 0
        n = bump_stop(where, key, session)
    except OSError as e:
        print(f"{key}: cannot keep the block count ({e}); ending the turn.", file=sys.stderr)
        return 0
    try:
        limit = int(load_config(root)["max_stop_blocks"])
    except Exception:
        limit = DEFAULTS["max_stop_blocks"]
    if n > limit:
        # The count starts again, so the next turn of this session (a fix round) is gated again.
        try:
            reset_stop(where, key, session)
        except OSError:
            pass
        print(f"{key} still failing after {limit} blocks; ending the turn. Report the failure in your reply: "
              f"{reason.splitlines()[0]}", file=sys.stderr)
    else:
        hook_block(f"{reason} (block {n}/{limit})")
    return 0
