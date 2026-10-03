# tests/test_ticket_gate.py
import json

import ticket_gate
from conftest import branch, commit, git, write_config


def gate(repo, capsys, *extra):
    code = ticket_gate.main(["--base", "main", "--root", str(repo), *extra])
    lines = capsys.readouterr().out.strip().splitlines()
    return code, {ln.split(" ", 2)[1].rstrip(":"): ln for ln in lines}


def prepare(repo, areas=("src/app/",), acs=("AC-7-1",), **cfg):
    base = dict(typecheck="true", lint="true", full_suite="true", ac_cmd="true")
    base.update(cfg)
    write_config(repo, **base)
    commit(repo, {".gitignore": ".fl-ticket.json\n.fl-stop-count.json\n"}, "ignore")
    branch(repo, "ticket/7-x")
    (repo / ".fl-ticket.json").write_text(json.dumps({"base": "main", "areas": list(areas), "acs": list(acs)}))


def test_clean_change_passes(repo, capsys):
    prepare(repo)
    commit(repo, {"src/app/core.py": "def f():\n    return 2\n", "tests/test_core.py":
                  "def test_a():\n    pass\n\ndef test_b():\n    pass\n\ndef test_c():\n    pass\n"})
    code, r = gate(repo, capsys)
    assert code == 0, r
    assert r["areas"].startswith("PASS") and r["typecheck"].startswith("PASS")
    assert r["mutation"].startswith("SKIP") and r["boundaries"].startswith("SKIP")


def test_out_of_area_change_fails_and_skips_commands(repo, capsys):
    prepare(repo)
    commit(repo, {"src/other/x.py": "X = 2\n"})
    code, r = gate(repo, capsys)
    assert code == 1
    assert r["areas"].startswith("FAIL") and "src/other/x.py" in r["areas"]
    assert r["typecheck"].startswith("SKIP")


def test_edited_acceptance_test_fails(repo, capsys):
    prepare(repo)
    commit(repo, {"tests/acceptance/test_ac.py": "def test_AC_7_1_flow():\n    assert True\n"})
    code, r = gate(repo, capsys)
    assert code == 1 and "tests/acceptance/test_ac.py" in r["protected"]


def test_added_skip_fails(repo, capsys):
    prepare(repo)
    commit(repo, {"tests/test_core.py": "import pytest\n\ndef test_a():\n    pass\n\n"
                  "@pytest.mark.skip\ndef test_b():\n    pass\n"})
    code, r = gate(repo, capsys)
    assert code == 1 and r["tests"].startswith("FAIL")


def test_failing_command_reports_output(repo, capsys):
    prepare(repo, typecheck="echo 'type error at a.py:3'; exit 1")
    commit(repo, {"src/app/core.py": "def f():\n    return 3\n"})
    code, r = gate(repo, capsys)
    assert code == 1 and "type error at a.py:3" in r["typecheck"]


def test_acceptance_runs_ticket_and_regression_ids(repo, capsys):
    prepare(repo, ac_cmd="echo {ids_k} > ac.txt")
    commit(repo, {"src/app/core.py": "def f():\n    return 4\n"})
    code, _ = gate(repo, capsys, "--acs", "AC-3-1,AC-3-2")
    assert code == 0
    assert (repo / "ac.txt").read_text().strip() == "AC_3_1 or AC_3_2 or AC_7_1"


def test_ticket_argument_beats_a_rewritten_worktree_copy(repo, capsys, tmp_path):
    prepare(repo)
    (repo / ".fl-ticket.json").write_text(json.dumps({"base": "main", "areas": ["src/"], "acs": ["AC-7-1"]}))
    real = tmp_path / "ticket.json"
    real.write_text(json.dumps({"base": "main", "areas": ["src/app/"], "acs": ["AC-7-1"]}))
    commit(repo, {"src/other/x.py": "X = 3\n"})
    code, r = gate(repo, capsys, "--ticket", str(real))
    assert code == 1 and r["areas"].startswith("FAIL")


def test_mutation_gets_changed_source_files(repo, capsys):
    prepare(repo, mutation_cmd="echo {files} > mut.txt")
    commit(repo, {"src/app/core.py": "def f():\n    return 5\n"})
    code, r = gate(repo, capsys)
    assert code == 0 and r["mutation"].startswith("PASS")
    assert (repo / "mut.txt").read_text().strip() == "src/app/core.py"


def test_rename_out_of_another_area_fails_areas(repo, capsys):
    prepare(repo)
    git(repo, "mv", "src/other/x.py", "src/app/x.py")
    git(repo, "commit", "-q", "-m", "move")
    code, r = gate(repo, capsys)
    assert code == 1 and r["areas"].startswith("FAIL") and "src/other/x.py" in r["areas"]


def test_new_test_runner_config_fails_protected(repo, capsys):
    prepare(repo, areas=())
    commit(repo, {"pytest.ini": "[pytest]\naddopts = --deselect tests/acceptance\n",
                  "web/vitest.config.ts": "export default {}\n", ".coveragerc": "[report]\nfail_under = 1\n"})
    code, r = gate(repo, capsys)
    assert code == 1 and r["protected"].startswith("FAIL")
    assert all(p in r["protected"] for p in ("pytest.ini", "web/vitest.config.ts", ".coveragerc"))


def test_new_conftest_is_not_protected(repo, capsys):
    prepare(repo)
    commit(repo, {"tests/conftest.py": "import pytest\n"})
    code, r = gate(repo, capsys)
    assert code == 0 and r["protected"].startswith("PASS")


PYPROJECT = ('[project]\nname = "x"\nversion = "1"\n\n[tool.pytest.ini_options]\naddopts = "-q"\n\n'
             "[tool.coverage.report]\nfail_under = 90\n")


def test_pytest_and_coverage_tables_in_pyproject_are_protected(repo, capsys):
    commit(repo, {"pyproject.toml": PYPROJECT})
    prepare(repo, areas=())
    commit(repo, {"pyproject.toml": PYPROJECT.replace('version = "1"', 'version = "2"')})
    code, r = gate(repo, capsys)
    assert code == 0 and r["protected"].startswith("PASS")
    commit(repo, {"pyproject.toml": PYPROJECT.replace('"-q"', '"-q --deselect tests/acceptance"')})
    code, r = gate(repo, capsys)
    assert code == 1 and "pyproject.toml" in r["protected"] and "[tool.pytest]" in r["protected"]
    commit(repo, {"pyproject.toml": PYPROJECT.replace("fail_under = 90", "fail_under = 10")})
    code, r = gate(repo, capsys)
    assert code == 1 and "[tool.coverage]" in r["protected"] and "[tool.pytest]" not in r["protected"]


def test_new_pytest_table_in_a_new_pyproject_is_protected_unless_allowed(repo, capsys):
    prepare(repo, areas=())
    commit(repo, {"svc/pyproject.toml": '[tool.pytest.ini_options]\naddopts = "-k nothing"\n'})
    code, r = gate(repo, capsys)
    assert code == 1 and "svc/pyproject.toml" in r["protected"]
    (repo / ".fl-ticket.json").write_text(json.dumps({"base": "main", "acs": ["AC-7-1"],
                                                     "allow": ["svc/pyproject.toml"]}))
    code, r = gate(repo, capsys)
    assert code == 0 and r["protected"].startswith("PASS")


SETUP_CFG = "[metadata]\nname = x\n\n[tool:pytest]\naddopts = -q\n\n[coverage:report]\nfail_under = 90\n"
TOX_INI = "[tox]\nenvlist = py\n\n[pytest]\naddopts = -q\n"


def test_pytest_and_coverage_sections_in_setup_cfg_and_tox_ini_are_protected(repo, capsys):
    commit(repo, {"setup.cfg": SETUP_CFG, "tox.ini": TOX_INI})
    prepare(repo, areas=())
    commit(repo, {"setup.cfg": SETUP_CFG.replace("name = x", "name = y"),
                  "tox.ini": TOX_INI.replace("envlist = py", "envlist = py313")})
    code, r = gate(repo, capsys)
    assert code == 0 and r["protected"].startswith("PASS")
    commit(repo, {"setup.cfg": SETUP_CFG.replace("fail_under = 90", "fail_under = 5"),
                  "tox.ini": TOX_INI.replace("addopts = -q", "addopts = -q -k nothing")})
    code, r = gate(repo, capsys)
    assert code == 1 and "setup.cfg [coverage:report]" in r["protected"] and "tox.ini [pytest]" in r["protected"]


def test_acs_without_ac_cmd_fail(repo, capsys):
    prepare(repo, ac_cmd="")
    commit(repo, {"src/app/core.py": "def f():\n    return 6\n"})
    code, r = gate(repo, capsys)
    assert code == 1 and r["acceptance"] == "FAIL acceptance: ac_cmd not configured"
    (repo / ".fl-ticket.json").write_text(json.dumps({"base": "main", "areas": ["src/app/"], "acs": []}))
    code, r = gate(repo, capsys)
    assert code == 0 and r["acceptance"].startswith("SKIP")


def test_uncommitted_changes_fail_the_gate_before_any_command(repo, capsys):
    prepare(repo)
    commit(repo, {"src/app/core.py": "def f():\n    return 7\n"})
    (repo / "scripts/claude/fl-config.json").write_text('{"full_suite": "true"}')
    (repo / "src/app/new.py").write_text("Y = 1\n")
    code, r = gate(repo, capsys)
    assert code == 1 and r["clean"].startswith("FAIL")
    assert "scripts/claude/fl-config.json" in r["clean"] and "src/app/new.py" in r["clean"]
    assert ".fl-ticket.json" not in r["clean"]
    assert r["typecheck"].startswith("SKIP")


def test_config_argument_beats_the_worktree_config(repo, capsys, tmp_path):
    prepare(repo)
    commit(repo, {"src/app/core.py": "def f():\n    return 8\n"})
    strict = tmp_path / "fl-config.json"
    strict.write_text(json.dumps({"typecheck": "true", "lint": "true", "full_suite": "echo strict suite; exit 1",
                                  "ac_cmd": "true"}))
    code, r = gate(repo, capsys, "--config", str(strict))
    assert code == 1 and "strict suite" in r["full"]
