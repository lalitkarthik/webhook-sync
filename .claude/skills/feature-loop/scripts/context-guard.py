#!/usr/bin/env python
"""Context budget guard for Claude Code sessions (feature-loop skill).

Hook mode (default, no arguments): reads the hook event JSON on stdin, works
out how full this session's context window is, and injects a notice into the
model's context when usage crosses 50% (warning) or 60% (hand off now).
The rule is in .claude/skills/feature-loop/workflow.md, "Context budget".

Report mode (--report): prints one line per recently active Claude session on
this machine (used %, branch, working directory, session id) so an
orchestrator can watch its implementers. Add --minutes N to widen the window.

Where the numbers come from, in order of preference:
  1. The status-line bridge file $TMPDIR/claude-ctx-<session_id>.json, written
     by the GSD status line (used_pct is Claude Code's own figure).
  2. The session transcript: the last assistant message's usage block
     (input + cache read + cache creation tokens) divided by the window size
     (FEATURE_LOOP_CONTEXT_WINDOW env var; by default 200k for Haiku, 1M
     otherwise). Marked "estimate".

Windows: runs under `python` (not `python3`) and reads/writes UTF-8
explicitly, since the console default code page cannot decode every prompt.
"""
import glob
import json
import os
import sys
import tempfile
import time

WARN_PCT = 50
HANDOFF_PCT = 60
DEBOUNCE_CALLS = 5      # tool uses between repeated hand-off notices
BRIDGE_STALE_SECONDS = 120
HANDOFF_DOC = ".claude/skills/feature-loop/prompts/handoff.md"
DEFAULT_WINDOW = int(os.environ.get("FEATURE_LOOP_CONTEXT_WINDOW", "0"))  # 0 = pick by model


def window_for(model):
    """Best-effort context window size when the status line is not available."""
    if DEFAULT_WINDOW:
        return DEFAULT_WINDOW
    m = (model or "").lower()
    if "haiku" in m:
        return 200_000
    return 1_000_000  # Claude 5 family sessions report 1M windows


def tmpdir():
    return tempfile.gettempdir()


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def write_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        pass


def usage_from_bridge(session_id, allow_stale=False):
    data = read_json(os.path.join(tmpdir(), f"claude-ctx-{session_id}.json"))
    if not data:
        return None
    ts = data.get("timestamp")
    age = time.time() - float(ts) if ts else 0
    if age > BRIDGE_STALE_SECONDS and not allow_stale:
        return None
    label = "status line" if age <= BRIDGE_STALE_SECONDS else f"status line {int(age // 60)}m old"
    if data.get("used_pct") is not None:
        return float(data["used_pct"]), label
    if data.get("remaining_percentage") is not None:
        return 100.0 - float(data["remaining_percentage"]), label
    return None


def last_assistant_entry(transcript_path, tail_bytes=4_000_000):
    """Return the last transcript entry of type 'assistant' that carries usage."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read().decode("utf-8", errors="ignore")
    except Exception:
        return None
    for line in reversed(chunk.splitlines()):
        if '"assistant"' not in line or '"usage"' not in line:
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        msg = entry.get("message") or {}
        if entry.get("type") == "assistant" and isinstance(msg, dict) and msg.get("usage"):
            return entry
    return None


def context_tokens(entry):
    usage = (entry.get("message") or {}).get("usage") or {}
    return (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )


def usage_from_transcript(transcript_path):
    entry = last_assistant_entry(transcript_path)
    if not entry:
        return None
    model = (entry.get("message") or {}).get("model")
    return context_tokens(entry) * 100.0 / window_for(model), "transcript estimate"


def notice(level, pct, source):
    if level == "handoff":
        return (
            f"[Context budget] This session has used {pct:.0f}% of its context window "
            f"({source}), over the 60% limit. Hand off now: follow "
            f"{HANDOFF_DOC} (format in docs/agents/handoff.md). Finish the current "
            "tool call, start nothing new, write the hand-off file, hand over to the "
            "successor, then end your turn."
        )
    return (
        f"[Context budget] This session has used {pct:.0f}% of its context window "
        f"({source}). The hard limit is 60%. Finish the current step, avoid reading "
        f"large files or long outputs, and get ready to hand off ({HANDOFF_DOC})."
    )


def emit(event_name, text):
    if event_name == "UserPromptSubmit":
        print(text)
    else:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": event_name or "PostToolUse",
                "additionalContext": text,
            }
        }))


def hook_mode():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
    except Exception:
        return
    if data.get("agent_id"):          # a subagent; its parent is what we budget
        return
    session_id = str(data.get("session_id") or "")
    if not session_id or "/" in session_id or "\\" in session_id or ".." in session_id:
        return
    usage = usage_from_bridge(session_id) or usage_from_transcript(data.get("transcript_path"))
    if not usage:
        return
    pct, source = usage
    level = "handoff" if pct >= HANDOFF_PCT else "warn" if pct >= WARN_PCT else None

    state_path = os.path.join(tmpdir(), f"feature-loop-ctx-guard-{session_id}.json")
    state = read_json(state_path) or {"level": None, "calls": 0}
    if level is None:
        write_json(state_path, {"level": None, "calls": 0})
        return
    state["calls"] = int(state.get("calls") or 0) + 1
    escalated = level != state.get("level")
    repeat = level == "handoff" and state["calls"] >= DEBOUNCE_CALLS
    if not (escalated or repeat):
        write_json(state_path, state)
        return
    write_json(state_path, {"level": level, "calls": 0})
    emit(data.get("hook_event_name"), notice(level, pct, source))


def report_mode(minutes):
    cutoff = time.time() - minutes * 60
    rows = []
    for path in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
        try:
            if os.path.getmtime(path) < cutoff:
                continue
        except OSError:
            continue
        session_id = os.path.splitext(os.path.basename(path))[0]
        entry = last_assistant_entry(path)
        if not entry:
            continue
        model = (entry.get("message") or {}).get("model") or "?"
        bridged = usage_from_bridge(session_id, allow_stale=True)
        if bridged:
            pct, source = bridged
        else:
            pct, source = context_tokens(entry) * 100.0 / window_for(model), "estimate"
        rows.append((pct, source, model, entry.get("gitBranch") or "?", entry.get("cwd") or "?", session_id))
    rows.sort(reverse=True)
    print(f"{'used%':>6}  {'source':<22} {'model':<18} {'branch':<24} cwd  [session]")
    for pct, source, model, branch, cwd, sid in rows:
        flag = "  <-- HAND OFF" if pct >= HANDOFF_PCT else ("  <-- warn" if pct >= WARN_PCT else "")
        print(f"{pct:>5.0f}%  {source:<22} {model[:18]:<18} {branch[:24]:<24} {cwd}  [{sid[:8]}]{flag}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if "--report" in sys.argv:
        mins = 30
        if "--minutes" in sys.argv:
            try:
                mins = int(sys.argv[sys.argv.index("--minutes") + 1])
            except (IndexError, ValueError):
                pass
        report_mode(mins)
    else:
        hook_mode()
