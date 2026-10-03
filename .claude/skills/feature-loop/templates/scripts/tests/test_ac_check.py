# tests/test_ac_check.py
import shutil

import ac_check
from conftest import write_config

BODY = "## Acceptance criteria\n- AC-7-1 user uploads a statement\n- AC-7-2 totals match\n"


def body(tmp_path):
    f = tmp_path / "body.md"
    f.write_text(BODY)
    return str(f)


def test_all_tagged_files_present(repo, tmp_path, capsys):
    assert ac_check.main(["--body-file", body(tmp_path), "--root", str(repo)]) == 0


def test_missing_id_fails(repo, tmp_path, capsys):
    shutil.rmtree(repo / "tests/golden/AC-7-2")
    assert ac_check.main(["--body-file", body(tmp_path), "--root", str(repo)]) == 1
    assert "MISSING AC-7-2" in capsys.readouterr().out


def test_no_ids_in_issue_fails(repo, tmp_path, capsys):
    f = tmp_path / "b.md"
    f.write_text("no criteria here")
    assert ac_check.main(["--body-file", str(f), "--root", str(repo)]) == 1


def test_expect_red_and_green(repo, tmp_path, capsys):
    write_config(repo, ac_cmd='test "{ids_space}" = "AC-7-2"')
    assert ac_check.main(["--body-file", body(tmp_path), "--root", str(repo), "--expect", "red"]) == 1
    out = capsys.readouterr().out
    assert "RED AC-7-1" in out and "WRONG AC-7-2: expected red, got green" in out
    write_config(repo, ac_cmd="exit 1")
    assert ac_check.main(["--body-file", body(tmp_path), "--root", str(repo), "--expect", "red"]) == 0


def test_no_test_ran_is_neither_red_nor_green(repo, tmp_path, capsys):
    cmds = ["exit 5", "exit 2", "exit 3", "exit 4", "echo '=== no tests ran in 0.01s ==='; exit 1",
            "echo 'collected 3 items / 3 deselected / 0 selected'; exit 1", "echo 'No test files found'; exit 1",
            "echo 'No tests found, exiting with code 0'"]
    for cmd in cmds:
        write_config(repo, ac_cmd=cmd)
        for expect in ("red", "green"):
            assert ac_check.main(["--body-file", body(tmp_path), "--root", str(repo), "--expect", expect]) == 1, cmd
            out = capsys.readouterr().out
            assert "NOTEST AC-7-1" in out and "NOTEST AC-7-2" in out, cmd
            assert "RED" not in out and "GREEN" not in out, cmd


def test_a_real_failure_is_still_red(repo, tmp_path, capsys):
    write_config(repo, ac_cmd="echo '1 failed, 10 selected'; exit 1")
    assert ac_check.main(["--body-file", body(tmp_path), "--root", str(repo), "--expect", "red"]) == 0
    assert "RED AC-7-1" in capsys.readouterr().out


def test_a_broken_ac_cmd_is_not_red(repo, tmp_path, capsys):
    for cmd in ("exit 124", "exit 126", "exit 127", "fl_no_such_test_runner -k {ids_k}"):
        write_config(repo, ac_cmd=cmd)
        for expect in ("red", "green"):
            assert ac_check.main(["--body-file", body(tmp_path), "--root", str(repo), "--expect", expect]) == 1, cmd
            out = capsys.readouterr().out
            assert "NOTEST AC-7-1" in out and "RED" not in out, cmd
