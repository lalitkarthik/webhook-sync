# tests/test_gate_stop.py
import io
import json

import gate_stop
from conftest import write_config


def run(repo, monkeypatch, capsys, session="s1"):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(repo), "session_id": session})))
    assert gate_stop.main() == 0
    out = capsys.readouterr()
    return (json.loads(out.out) if out.out.strip() else None), out.err


def ticket(repo, acs=("AC-7-1",)):
    (repo / ".fl-ticket.json").write_text(json.dumps({"base": "main", "acs": list(acs)}))


def test_silent_without_ticket_file(repo, monkeypatch, capsys):
    write_config(repo, typecheck="exit 1")
    assert run(repo, monkeypatch, capsys) == (None, "")


def test_blocks_on_first_failing_command_then_gives_up(repo, monkeypatch, capsys):
    write_config(repo, typecheck="true", lint="echo lint broke; exit 1", full_suite="true", max_stop_blocks=2)
    ticket(repo)
    for n in (1, 2):
        out, _ = run(repo, monkeypatch, capsys)
        assert out["decision"] == "block" and "lint" in out["reason"] and "lint broke" in out["reason"]
        assert f"block {n}/2" in out["reason"]
    out, err = run(repo, monkeypatch, capsys)
    assert out is None and "still failing" in err


def test_blocks_again_after_a_give_up_and_a_new_turn(repo, monkeypatch, capsys):
    write_config(repo, typecheck="echo still red; exit 1", max_stop_blocks=1)
    ticket(repo)
    assert run(repo, monkeypatch, capsys)[0]["decision"] == "block"
    out, err = run(repo, monkeypatch, capsys)
    assert out is None and "still failing" in err
    out, _ = run(repo, monkeypatch, capsys)
    assert out["decision"] == "block" and "block 1/1" in out["reason"]


def test_passes_and_resets_counter(repo, monkeypatch, capsys):
    write_config(repo, typecheck="true", ac_cmd="echo {ids_k} > ac.txt")
    ticket(repo, acs=("AC-7-2", "AC-7-1"))
    assert run(repo, monkeypatch, capsys) == (None, "")
    assert (repo / "ac.txt").read_text().strip() == "AC_7_1 or AC_7_2"


def test_gate_commands_skip_empty_and_acless():
    cmds = gate_stop.gate_commands({"typecheck": "tc", "lint": "", "full_suite": "fs", "ac_cmd": "ac {ids_k}"},
                                   {"acs": []})
    assert cmds == [("typecheck", "tc"), ("full suite", "fs")]


def test_acs_without_ac_cmd_block(repo, monkeypatch, capsys):
    write_config(repo, typecheck="true")
    ticket(repo)
    out, _ = run(repo, monkeypatch, capsys)
    assert out["decision"] == "block" and "FAIL acceptance: ac_cmd not configured" in out["reason"]
    ticket(repo, acs=())
    assert run(repo, monkeypatch, capsys) == (None, "")


def test_broken_config_blocks_and_still_counts_toward_the_limit(repo, monkeypatch, capsys):
    ticket(repo)
    (repo / "scripts/claude/fl-config.json").write_text("{not json")
    for n in (1, 2, 3):
        out, _ = run(repo, monkeypatch, capsys)
        assert out["decision"] == "block" and "JSONDecodeError" in out["reason"]
        assert f"block {n}/3" in out["reason"]
    out, err = run(repo, monkeypatch, capsys)
    assert out is None and "ending the turn" in err


def test_broken_ticket_blocks_instead_of_crashing(repo, monkeypatch, capsys):
    write_config(repo, typecheck="true")
    (repo / ".fl-ticket.json").write_text("{not json")
    out, _ = run(repo, monkeypatch, capsys)
    assert out["decision"] == "block" and "JSONDecodeError" in out["reason"]
    (repo / ".fl-ticket.json").write_text("[1]")
    assert run(repo, monkeypatch, capsys)[0]["decision"] == "block"


def test_corrupt_stop_counter_does_not_crash_the_hook(repo, monkeypatch, capsys):
    write_config(repo, typecheck="exit 1")
    ticket(repo)
    (repo / ".fl-stop-count.json").write_text("{not json")
    out, _ = run(repo, monkeypatch, capsys)
    assert out["decision"] == "block" and "block 1/3" in out["reason"]


def test_unreadable_hook_input_blocks_and_is_counted_outside_the_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate_stop.fl.tempfile, "tempdir", str(tmp_path))
    for n in (1, 2, 3):
        monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
        assert gate_stop.main() == 0
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "block" and f"block {n}/3" in out["reason"]
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert gate_stop.main() == 0
    assert capsys.readouterr().out == ""
    assert (tmp_path / ".fl-stop-count.json").is_file()
