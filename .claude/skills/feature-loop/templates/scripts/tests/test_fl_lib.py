# tests/test_fl_lib.py
import fl_checks
import fl_lib as fl
from conftest import branch, commit


def test_matches_handles_double_star_and_root_files():
    assert fl.matches("tests/acceptance/a/b.py", ["tests/acceptance/**"])
    assert fl.matches("Dockerfile", ["**/Dockerfile"])
    assert fl.matches("svc/Dockerfile", ["**/Dockerfile"])
    assert fl.matches(".env.local", ["**/.env*"])
    assert fl.matches("uv.lock", ["**/uv.lock"])
    assert not fl.matches("src/tests_helper.py", ["tests/**"])
    assert fl.matches(".\\tests\\x.py".replace("\\", "/"), ["tests/**"])


def test_ac_ids_normalise_both_spellings():
    assert fl.ac_ids("def test_AC_7_1_x(): ...\ntest('AC-12-3 export')") == {"AC-7-1", "AC-12-3"}


def test_count_tests_python_and_js():
    py = "def test_a():\n    pass\nasync def test_b():\n    pass\ndef helper():\n    pass\n"
    js = "it('a', () => {})\ntest('b', () => {})\ntest.each([1])('c', () => {})\n"
    assert fl.count_tests(py) == 2
    assert fl.count_tests(js) == 3


def test_skip_re_catches_skips_and_suppressions():
    for line in ["it.skip('x')", "test.only('x')", "@pytest.mark.skip", "@pytest.mark.xfail(reason='x')",
                 "// @ts-ignore", "/* eslint-disable */", "x = 1  # type: ignore", "y = 2  # noqa: E501",
                 "  retries: 2,"]:
        assert fl.SKIP_RE.search(line), line
    assert not fl.SKIP_RE.search("retries: 0")


def test_format_cmd_fills_placeholders():
    t = 'pytest -k "{ids_k}" && pw -g "{ids_pipe}" && echo {ids_space} {files}'
    out = fl.format_cmd(t, ids=["AC-7-2", "AC-7-1"], files=["src/a b.py"])
    assert out == "pytest -k \"AC_7_1 or AC_7_2\" && pw -g \"AC-7-1|AC-7-2\" && echo AC-7-1 AC-7-2 'src/a b.py'"


def test_config_defaults_and_override(repo):
    from conftest import write_config
    write_config(repo, max_lines=10)
    cfg = fl.load_config(repo)
    assert cfg["max_lines"] == 10 and cfg["max_files"] == 25


def test_diff_helpers(repo):
    branch(repo, "ticket/7-x")
    commit(repo, {"src/app/core.py": "def f():\n    return 2\n", "src/app/new.py": "N = 1\n",
                  "src/other/x.py": None})
    mb = fl.merge_base("main", repo)
    ns = {(s, p) for s, p, _ in fl.name_status(mb, repo)}
    assert ns == {("M", "src/app/core.py"), ("A", "src/app/new.py"), ("D", "src/other/x.py")}
    stats = {p: (a, d) for a, d, p in fl.numstat(mb, repo)}
    assert stats["src/app/core.py"] == (1, 1)
    assert fl.added_lines(mb, repo)["src/app/new.py"] == ["N = 1"]
    assert fl.exists_at(mb, "src/other/x.py", repo) and not fl.exists_at("HEAD", "src/other/x.py", repo)


def test_run_cmd_returns_code_and_tail(repo):
    code, out = fl.run_cmd("echo hello; exit 3", repo)
    assert code == 3 and out == "hello"


def test_stop_counter(repo):
    assert fl.bump_stop(repo, "gate", "s1") == 1
    assert fl.bump_stop(repo, "gate", "s1") == 2
    fl.reset_stop(repo, "gate", "s1")
    assert fl.bump_stop(repo, "gate", "s1") == 1


def test_port_is_stable_and_in_range(repo):
    assert fl.port_for(repo) == fl.port_for(repo)
    assert 3000 <= fl.port_for(repo) < 4000


def test_checks_catch_weakened_tests(repo):
    cfg = fl.load_config(repo)
    branch(repo, "ticket/7-x")
    commit(repo, {"tests/test_core.py": "def test_a():\n    pass\n",
                  "src/app/core.py": "def f():  # type: ignore\n    return 1\n"})
    mb = fl.merge_base("main", repo)
    probs = fl_checks.test_problems(mb, repo, cfg, fl.name_status(mb, repo), fl.added_lines(mb, repo))
    assert any("test count dropped: 2 -> 1" in p for p in probs)
    assert any("suppression" in p and "src/app/core.py" in p for p in probs)


def test_checks_catch_deleted_test_file(repo):
    cfg = fl.load_config(repo)
    branch(repo, "ticket/7-x")
    commit(repo, {"tests/test_core.py": None})
    mb = fl.merge_base("main", repo)
    probs = fl_checks.test_problems(mb, repo, cfg, fl.name_status(mb, repo), {})
    assert any("test file deleted: tests/test_core.py" in p for p in probs)


def test_protected_existing_vs_new_vs_allowed(repo):
    cfg = fl.load_config(repo)
    branch(repo, "ticket/7-x")
    commit(repo, {"tests/acceptance/test_ac.py": "def test_AC_7_1_flow():\n    assert True\n",
                  "tests/acceptance/test_new.py": "def test_AC_7_3_x():\n    pass\n",
                  "uv.lock": "x\n"})
    mb = fl.merge_base("main", repo)
    ns = fl.name_status(mb, repo)
    assert fl_checks.protected_problems(mb, repo, cfg, ns, []) == [
        "protected file changed: tests/acceptance/test_ac.py"]
    assert fl_checks.protected_problems(mb, repo, cfg, ns, ["tests/acceptance/test_ac.py"]) == []


def test_always_protected_even_when_new(repo):
    cfg = fl.load_config(repo)
    branch(repo, "ticket/7-x")
    commit(repo, {"scripts/claude/evil.py": "x = 1\n"})
    mb = fl.merge_base("main", repo)
    assert fl_checks.protected_problems(mb, repo, cfg, fl.name_status(mb, repo), []) == [
        "protected file changed: scripts/claude/evil.py"]


def test_size_problem_ignores_lockfiles():
    cfg = dict(fl.DEFAULTS, max_lines=10, max_files=2)
    assert fl_checks.size_problem(cfg, [(500, 0, "uv.lock"), (5, 0, "a.py")]) is None
    assert "too big" in fl_checks.size_problem(cfg, [(6, 5, "a.py")])
    assert "3 files" in fl_checks.size_problem(cfg, [(1, 0, "a.py"), (1, 0, "b.py"), (1, 0, "c.py")])
    assert fl_checks.size_problem(dict(cfg, pr_max_lines=50, pr_max_files=5), [(30, 0, "a.py")],
                                  prefix="pr_") is None
