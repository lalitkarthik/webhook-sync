# tests/test_verdict.py
import io
import json

import evidence_check
import fl_lib as fl
import verdict


def good(folder, sha="abc1234"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "AC-7-1.yml").write_text("- button \"Export\"\n- status: Exported 3 rows\n")
    (folder / "AC-7-1.png").write_bytes(b"\x89PNG\r\n")
    return {"sha": sha, "patch_id": "", "overall": "PASS",
            "ac": [{"id": "AC-7-1", "status": "PASS", "evidence": ["AC-7-1.yml", "AC-7-1.png"],
                    "observed": "Exported 3 rows", "repro": ""}]}


def test_good_verdict_has_no_problems(tmp_path):
    assert verdict.validate(good(tmp_path), "abc1234", tmp_path) == []


def test_sha_mismatch(tmp_path):
    assert any("head is def5678" in p for p in verdict.validate(good(tmp_path), "def5678", tmp_path))


def test_observed_text_must_be_in_evidence(tmp_path):
    v = good(tmp_path)
    v["ac"][0]["observed"] = "Exported 4 rows"
    assert any("observed" in p for p in verdict.validate(v, None, tmp_path))


def test_image_only_evidence_is_not_enough(tmp_path):
    v = good(tmp_path)
    v["ac"][0]["evidence"] = ["AC-7-1.png"]
    assert any("text evidence" in p for p in verdict.validate(v, None, tmp_path))


def test_fail_needs_repro_and_overall_must_agree(tmp_path):
    v = good(tmp_path)
    v["ac"].append({"id": "AC-7-2", "status": "FAIL", "evidence": [], "observed": "", "repro": ""})
    probs = verdict.validate(v, None, tmp_path)
    assert any("AC-7-2: FAIL without repro" in p for p in probs)
    assert any("overall must be FAIL" in p for p in probs)


def test_unknown_status(tmp_path):
    v = good(tmp_path)
    v["ac"][0]["status"] = "OK"
    assert any("status must be one of" in p for p in verdict.validate(v, None, tmp_path))


def run_hook(repo, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"cwd": str(repo), "session_id": "v1"})))
    assert evidence_check.main() == 0
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else None


def test_hook_blocks_without_verdict_then_accepts_good_one(repo, tmp_path, monkeypatch, capsys):
    ev = tmp_path / "ev"
    (repo / ".fl-ticket.json").write_text(json.dumps({"base": "main", "evidence_dir": ev.as_posix()}))
    out = run_hook(repo, monkeypatch, capsys)
    assert out["decision"] == "block" and "verdict.json" in out["reason"]
    head = fl.git(["rev-parse", "HEAD"], repo).strip()
    (ev / "verdict.json").write_text(json.dumps(good(ev, sha=head)))
    assert run_hook(repo, monkeypatch, capsys) is None


def test_hook_silent_outside_verifier_runs(repo, monkeypatch, capsys):
    (repo / ".fl-ticket.json").write_text(json.dumps({"base": "main"}))
    assert run_hook(repo, monkeypatch, capsys) is None


def test_expected_ids_must_all_be_present(tmp_path):
    v = good(tmp_path)
    assert verdict.validate(v, None, tmp_path, ["AC-7-1"]) == []
    probs = verdict.validate(v, None, tmp_path, ["AC-7-1", "AC-7-2", "AC-7-3"])
    assert any("AC-7-2" in p and "AC-7-3" in p and "leaves out" in p for p in probs)


def test_duplicate_ids_are_rejected(tmp_path):
    v = good(tmp_path)
    v["ac"].append(dict(v["ac"][0]))
    assert any("AC-7-1" in p and "more than once" in p for p in verdict.validate(v, None, tmp_path))


def test_empty_ac_list_is_never_a_pass(tmp_path):
    for ac in ([], None):
        probs = verdict.validate({"sha": "abc1234", "overall": "PASS", "ac": ac}, "abc1234", tmp_path)
        assert any("no acceptance criteria" in p for p in probs) and "overall must be FAIL" in probs
        assert verdict.validate({"sha": "abc1234", "overall": "FAIL", "ac": ac}, "abc1234", tmp_path)


def test_bad_shapes_are_problems_not_exceptions(tmp_path):
    assert verdict.validate([good(tmp_path)], None, tmp_path)
    assert verdict.validate("PASS", None, tmp_path)
    for value in (["AC-7-1"], {"id": "AC-7-1"}, "AC-7-1"):
        v = good(tmp_path)
        v["ac"] = value
        assert verdict.validate(v, None, tmp_path), value
    for field, value in [("status", []), ("status", {}), ("id", 7), ("id", ""), ("evidence", "AC-7-1.yml"),
                         ("evidence", [1]), ("evidence", [["AC-7-1.yml"]]), ("observed", ["Exported 3 rows"])]:
        v = good(tmp_path)
        v["ac"][0][field] = value
        assert verdict.validate(v, None, tmp_path), (field, value)
    v = good(tmp_path)
    v["ac"][0].update(status="FAIL", repro=["step"])
    v["overall"] = "FAIL"
    assert any("repro" in p for p in verdict.validate(v, None, tmp_path))


def verifier_ticket(repo, ev, **extra):
    (repo / ".fl-ticket.json").write_text(json.dumps({"base": "main", "evidence_dir": ev.as_posix(), **extra}))
    return fl.git(["rev-parse", "HEAD"], repo).strip()


def test_hook_blocks_a_verdict_that_leaves_out_a_ticket_ac(repo, tmp_path, monkeypatch, capsys):
    ev = tmp_path / "ev"
    head = verifier_ticket(repo, ev, acs=["AC-7-1", "AC-7-2"])
    (ev / "verdict.json").parent.mkdir(parents=True)
    (ev / "verdict.json").write_text(json.dumps(good(ev, sha=head)))
    out = run_hook(repo, monkeypatch, capsys)
    assert out["decision"] == "block" and "AC-7-2" in out["reason"]
    verifier_ticket(repo, ev, acs=["AC-7-1"])
    assert run_hook(repo, monkeypatch, capsys) is None
    verifier_ticket(repo, ev, acs=[])
    assert run_hook(repo, monkeypatch, capsys) is None


def test_hook_blocks_a_badly_shaped_verdict_instead_of_crashing(repo, tmp_path, monkeypatch, capsys):
    ev = tmp_path / "ev"
    head = verifier_ticket(repo, ev)
    ev.mkdir(parents=True)
    for bad in ({"sha": head, "overall": "PASS", "ac": ["AC-7-1"]}, [1],
                {"sha": head, "overall": "PASS", "ac": [{"id": "AC-7-1", "status": []}]}):
        (ev / "verdict.json").write_text(json.dumps(bad))
        assert run_hook(repo, monkeypatch, capsys)["decision"] == "block", bad


def test_hook_crash_blocks_and_still_counts_toward_the_limit(repo, monkeypatch, capsys):
    (repo / ".fl-ticket.json").write_text("{not json")
    for n in (1, 2, 3):
        out = run_hook(repo, monkeypatch, capsys)
        assert out["decision"] == "block" and "JSONDecodeError" in out["reason"] and f"block {n}/3" in out["reason"]
    assert run_hook(repo, monkeypatch, capsys) is None


def cli(folder, capsys, *extra):
    code = verdict.main(["--dir", str(folder), *extra])
    return code, capsys.readouterr().out


def test_cli_accepts_a_good_verdict_pinned_to_the_sha(tmp_path, capsys):
    (tmp_path / "verdict.json").write_text(json.dumps(good(tmp_path)))
    code, out = cli(tmp_path, capsys, "--sha", "abc1234", "--acs", "AC-7-1")
    assert code == 0 and out.startswith("PASS")


def test_cli_reports_a_valid_fail_with_its_entries(tmp_path, capsys):
    v = good(tmp_path)
    v["ac"][0].update(status="FAIL", repro="click Export: nothing happens")
    v["overall"] = "FAIL"
    (tmp_path / "verdict.json").write_text(json.dumps(v))
    code, out = cli(tmp_path, capsys, "--sha", "abc1234", "--acs", "AC-7-1")
    assert code == 1 and out.startswith("FAIL") and "AC-7-1 FAIL" in out and "nothing happens" in out


def test_cli_refuses_a_pass_the_evidence_does_not_back(tmp_path, capsys):
    v = good(tmp_path)
    v["ac"][0]["observed"] = "Exported 4 rows"
    (tmp_path / "verdict.json").write_text(json.dumps(v))
    code, out = cli(tmp_path, capsys, "--sha", "abc1234", "--acs", "AC-7-1")
    assert code == 1 and out.startswith("INVALID") and "observed" in out


def test_cli_refuses_a_stale_sha_a_missing_ac_and_a_missing_file(tmp_path, capsys):
    (tmp_path / "verdict.json").write_text(json.dumps(good(tmp_path)))
    code, out = cli(tmp_path, capsys, "--sha", "def5678", "--acs", "AC-7-1,AC-7-2")
    assert code == 1 and out.startswith("INVALID") and "head is def5678" in out and "AC-7-2" in out
    code, out = cli(tmp_path / "nowhere", capsys, "--sha", "abc1234")
    assert code == 1 and out.startswith("INVALID") and "verdict.json" in out
