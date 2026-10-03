"""SubagentStop hook for fl-impl-* agents: no finishing while the ticket's gate is red.

Runs typecheck, lint, the full suite and the ticket's acceptance tests, stopping at the first
failure. After max_stop_blocks blocks in one session it lets the turn end, so it cannot loop.
A crash of the hook itself blocks too, and counts toward the same limit.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fl_lib as fl  # noqa: E402


def gate_commands(cfg: dict, ticket: dict) -> list[tuple[str, str]]:
    acs = ticket.get("acs") or []
    cmds = [("typecheck", cfg.get("typecheck", "")), ("lint", cfg.get("lint", "")),
            ("full suite", cfg.get("full_suite", "")),
            ("acceptance", fl.format_cmd(cfg.get("ac_cmd", ""), ids=acs) if acs else "")]
    return [(n, c) for n, c in cmds if c]


def check(data: dict, root: Path) -> str | None:
    ticket = fl.load_ticket(root)
    if not ticket:
        return None
    cfg = fl.load_config(root)
    for name, cmd in gate_commands(cfg, ticket):
        code, out = fl.run_cmd(cmd, root)
        if code != 0:
            return f"Gate failed: {name} (`{cmd}`)\n{out}\nFix it, commit, then finish."
    if ticket.get("acs") and not cfg.get("ac_cmd"):
        return ("Gate failed.\nFAIL acceptance: ac_cmd not configured\nThe ticket lists acceptance criteria, but "
                "scripts/claude/fl-config.json has no ac_cmd to run them. You may not edit that file: report "
                "this in your reply.")
    return None


def main() -> int:
    return fl.run_stop_hook("gate", check)


if __name__ == "__main__":
    sys.exit(main())
