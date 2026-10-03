"""PreToolUse hook (Edit|Write|NotebookEdit) for fl-impl-* agents.

Denies edits to protected files that already exist on the ticket's base branch, and always, also
when the file is new, to scripts/claude, .claude, .fl-ticket.json and test-runner or coverage config
(pytest.ini, playwright/vitest/jest config, .coveragerc). The ticket gate re-checks the diff, so a
change made another way (a Bash redirect) is still caught there; so are the pytest and coverage
sections of pyproject.toml, setup.cfg and tox.ini, which this hook cannot judge before the edit.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fl_lib as fl  # noqa: E402


def decide(file_path: str, root: Path, cfg: dict, ticket: dict) -> str | None:
    try:
        rel = Path(file_path).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return None
    if fl.matches(rel, ticket.get("allow", [])):
        return None
    if fl.matches(rel, cfg["always_protected"]):
        if fl.matches(rel, fl.TEST_CONFIG_GLOBS):
            return (f"{rel} is test-runner or coverage config: a worker may neither edit nor create it. "
                    f"Make the code pass the tests as configured; if the config is wrong, reply QUESTION.")
        return f"{rel} is part of the feature-loop harness and cannot be edited by a worker."
    if fl.matches(rel, cfg["protected_globs"]) and fl.exists_at(ticket.get("base") or "HEAD", rel, root):
        return (f"{rel} is protected: acceptance tests, golden files, snapshots, test config and lockfiles "
                f"are owned by the test author. Make the code pass them; if one is wrong, reply QUESTION.")
    return None


def check(data: dict) -> str | None:
    tool_input = data.get("tool_input") or {}
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not path:
        return None
    try:
        root = fl.repo_root(data.get("cwd") or ".")
    except RuntimeError:
        return None
    if not Path(path).is_absolute():
        path = str(Path(data.get("cwd") or root) / path)
    return decide(path, root, fl.load_config(root), fl.load_ticket(root))


def main() -> int:
    return fl.run_pretool_hook("protect_tests", check)


if __name__ == "__main__":
    sys.exit(main())
