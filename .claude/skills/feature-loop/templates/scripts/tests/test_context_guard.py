# tests/test_context_guard.py — the skill's own scripts/context-guard.py (setup copies it to every repo)
import importlib.util
import io
import json
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[3] / "scripts" / "context-guard.py"
_spec = importlib.util.spec_from_file_location("context_guard", GUARD)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


@pytest.mark.parametrize("pct, level", [(0, None), (34.9, None), (35, "warn"), (39.9, "warn"),
                                        (40, "handoff"), (44.9, "handoff"), (45, "hard"), (80, "hard")])
def test_warns_at_35_hands_off_at_40_and_is_over_the_limit_at_45(pct, level):
    assert guard.level_for(pct) == level


def test_notices_state_the_budget_and_never_round_up_to_the_next_level():
    assert "40%" in guard.notice("warn", 36, "x") and "45%" in guard.notice("warn", 36, "x")
    text = guard.notice("handoff", 44.9, "x")
    assert "used 44%" in text and "hand-off point is 40%" in text and "hard limit is 45%" in text
    assert "no session goes past 45%" in guard.notice("hard", 45.2, "x")


def transcript(tmp_path, tokens):
    t = tmp_path / "t.jsonl"
    entry = {"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": tokens}}}
    t.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    return t


def hook(tmp_path, monkeypatch, capsys, tokens):
    monkeypatch.setattr(guard, "tmpdir", lambda: str(tmp_path))
    monkeypatch.setattr(guard, "DEFAULT_WINDOW", 1_000_000)
    event = {"session_id": "s1", "hook_event_name": "PostToolUse",
             "transcript_path": str(transcript(tmp_path, tokens))}
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(event).encode())))
    guard.hook_mode()
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else None


def test_hook_hands_off_at_40_percent_of_the_window(tmp_path, monkeypatch, capsys):
    assert hook(tmp_path, monkeypatch, capsys, 340_000) is None
    assert "get ready to hand off" in hook(tmp_path, monkeypatch, capsys, 350_000)
    assert "Hand off now" in hook(tmp_path, monkeypatch, capsys, 400_000)
    assert "OVER THE HARD LIMIT" in hook(tmp_path, monkeypatch, capsys, 450_000)
