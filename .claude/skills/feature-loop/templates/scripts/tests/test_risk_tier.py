# tests/test_risk_tier.py
import pytest

import risk_tier
from conftest import branch, commit


def test_plain_change_is_auto(repo):
    branch(repo, "feature/x")
    commit(repo, {"src/app/core.py": "def f():\n    return 9\n"})
    assert risk_tier.assess("main", repo) == []


def test_sensitive_paths_hold(repo):
    branch(repo, "feature/x")
    commit(repo, {".github/workflows/ci.yml": "on: push\n", "src/auth/login.py": "X = 1\n"})
    reasons = risk_tier.assess("main", repo)
    assert any("sensitive paths" in r and ".github/workflows/ci.yml" in r and "src/auth/login.py" in r
               for r in reasons)


def test_lockfile_change_is_a_dependency_change(repo):
    branch(repo, "feature/x")
    commit(repo, {"uv.lock": "pkg = 1\n"})
    assert any(r.startswith("dependency change: uv.lock") for r in risk_tier.assess("main", repo))


def test_rule_config_added_lines_auto_removed_lines_hold(repo):
    commit(repo, {"ruff.toml": "[lint]\nselect = [\"E\"]\nignore = [\"E501\"]\n"})
    branch(repo, "feature/x")
    commit(repo, {"ruff.toml": "[lint]\nselect = [\"E\"]\nignore = [\"E501\"]\nextend-select = [\"B\"]\n"})
    assert risk_tier.assess("main", repo) == []
    commit(repo, {"ruff.toml": "[lint]\nselect = [\"E\"]\nextend-select = [\"B\"]\n"})
    assert any("rule config lines removed" in r for r in risk_tier.assess("main", repo))


def test_changed_golden_file_holds(repo):
    branch(repo, "feature/x")
    commit(repo, {"tests/golden/AC-7-2/expected.json": "{\"total\": 1}\n"})
    assert "protected file changed: tests/golden/AC-7-2/expected.json" in risk_tier.assess("main", repo)


def test_cli_exit_codes(repo, capsys):
    branch(repo, "feature/x")
    commit(repo, {"src/app/core.py": "def f():\n    return 8\n"})
    assert risk_tier.main(["--base", "main", "--root", str(repo)]) == 0
    assert capsys.readouterr().out.strip() == "auto"
    commit(repo, {"CLAUDE.md": "rules\n"})
    assert risk_tier.main(["--base", "main", "--root", str(repo)]) == 3
    assert capsys.readouterr().out.startswith("hold\n- sensitive paths: CLAUDE.md")


RUFF = '[lint]\nselect = ["E"]\n'
TSCONFIG = '{\n  "compilerOptions": {\n    "strict": true\n  }\n}\n'
ESLINT = "export default [\n  { rules: { 'no-var': 'error' } },\n]\n"


@pytest.mark.parametrize("path, before, after", [
    ("ruff.toml", RUFF, RUFF + 'extend-ignore = ["B"]\n'),
    ("ruff.toml", RUFF, RUFF + '[lint.per-file-ignores]\n"src/*" = ["E"]\n'),
    ("ruff.toml", RUFF, RUFF + 'extend-exclude = ["src"]\n'),
    ("tsconfig.json", TSCONFIG, TSCONFIG.replace("true\n", 'true,\n    "noImplicitAny": false\n')),
    ("tsconfig.json", TSCONFIG, TSCONFIG.replace("true\n", 'true,\n    "allowJs": true\n')),
    ("eslint.config.js", ESLINT, ESLINT.replace("]\n", "  { rules: { 'no-console': 'off' } },\n]\n")),
    ("eslint.config.js", ESLINT, ESLINT.replace("]\n", "  { rules: { 'no-console': 'warn' } },\n]\n")),
    ("pyproject.toml", "[tool.mypy]\nstrict = true\n", '[tool.mypy]\nstrict = true\ndisable_error_code = ["x"]\n'),
])
def test_relaxation_by_added_lines_holds(repo, path, before, after):
    commit(repo, {path: before})
    branch(repo, "feature/x")
    commit(repo, {path: after})
    reasons = risk_tier.assess("main", repo)
    assert any("relaxed by added lines" in r and path in r for r in reasons), reasons


def test_new_root_rule_config_with_an_empty_select_holds(repo):
    branch(repo, "feature/x")
    commit(repo, {"ruff.toml": "[lint]\nselect = []\n"})
    assert any("relaxed by added lines" in r for r in risk_tier.assess("main", repo))


def test_new_root_rule_config_that_only_adds_rules_is_auto(repo):
    branch(repo, "feature/x")
    commit(repo, {"ruff.toml": '[lint]\nselect = ["E", "B"]\n',
                  ".importlinter": "[importlinter]\nroot_package = app\n"})
    assert risk_tier.assess("main", repo) == []


def test_new_nested_rule_config_holds(repo):
    branch(repo, "feature/x")
    commit(repo, {"src/app/ruff.toml": '[lint]\nselect = ["E"]\n'})
    reasons = risk_tier.assess("main", repo)
    assert any("new nested rule config" in r and "src/app/ruff.toml" in r for r in reasons), reasons


@pytest.mark.parametrize("path", ["src/api/session.py", "lib/jwt.ts", "webhooks/signature.py", "src/hmac_check.py",
                                  "web/lib/session-store.ts"])
def test_session_jwt_and_signature_code_holds(repo, path):
    branch(repo, "feature/x")
    commit(repo, {path: "X = 1\n"})
    assert any("sensitive paths" in r and path in r for r in risk_tier.assess("main", repo))


def test_plain_webhook_code_is_auto(repo):
    branch(repo, "feature/x")
    commit(repo, {"src/webhooks/handler.py": "X = 1\n"})
    assert risk_tier.assess("main", repo) == []


def test_changed_pytest_table_or_new_test_config_holds(repo):
    commit(repo, {"pyproject.toml": '[tool.pytest.ini_options]\naddopts = "-q"\n'})
    branch(repo, "feature/x")
    commit(repo, {"pyproject.toml": '[tool.pytest.ini_options]\naddopts = "-q -x"\n'})
    assert "protected test config changed: pyproject.toml [tool.pytest]" in risk_tier.assess("main", repo)
    commit(repo, {"pytest.ini": "[pytest]\n"})
    assert "protected file changed: pytest.ini" in risk_tier.assess("main", repo)


MYPY = "[tool.mypy]\nstrict = true\n"
PYRIGHT = '{\n  "typeCheckingMode": "strict"\n}\n'


@pytest.mark.parametrize("line", ["disallow_untyped_defs = true", "warn_return_any = true",
                                  "disallow_any_generics = true", "warn_unreachable = true"])
def test_adding_a_stricter_rule_with_allow_or_warn_in_its_name_is_auto(repo, line):
    commit(repo, {"pyproject.toml": MYPY})
    branch(repo, "feature/x")
    commit(repo, {"pyproject.toml": MYPY + line + "\n"})
    assert risk_tier.assess("main", repo) == []


@pytest.mark.parametrize("path, before, after", [
    ("pyproject.toml", MYPY, MYPY + "allow_untyped_defs = true\n"),
    ("pyproject.toml", MYPY, MYPY + "allow-untyped = true\n"),
    ("pyproject.toml", MYPY, MYPY + "disallow_untyped_defs = false\n"),
    ("eslint.config.js", ESLINT, ESLINT.replace("]\n", '  { rules: { "no-console": ["warn", {}] } },\n]\n')),
    ("pyrightconfig.json", PYRIGHT, PYRIGHT.replace('"strict"\n', '"strict",\n  "reportMissingImports": "warning"\n')),
])
def test_allow_and_warn_relaxations_still_hold(repo, path, before, after):
    commit(repo, {path: before})
    branch(repo, "feature/x")
    commit(repo, {path: after})
    reasons = risk_tier.assess("main", repo)
    assert any("relaxed by added lines" in r and path in r for r in reasons), reasons
