# tests/test_ci_check.py
import ci_check
from conftest import write_config


def test_runs_every_tagged_ac_by_default(repo):
    write_config(repo, typecheck="true", ac_cmd="echo {ids_space} > ac.txt")
    assert ci_check.main(["--root", str(repo)]) == 0
    assert (repo / "ac.txt").read_text().strip() == "AC-7-1 AC-7-2"


def test_only_the_given_acs(repo):
    write_config(repo, ac_cmd="echo {ids_space} > ac.txt")
    assert ci_check.main(["--root", str(repo), "--acs", "AC-7-1"]) == 0
    assert (repo / "ac.txt").read_text().strip() == "AC-7-1"


def test_smoke_failure_fails(repo, capsys):
    write_config(repo, smoke_cmd="echo down; exit 1")
    assert ci_check.main(["--root", str(repo), "--only", "smoke"]) == 1
    assert "FAIL smoke" in capsys.readouterr().out
