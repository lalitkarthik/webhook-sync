"""Checks shared by ticket_gate.py and risk_tier.py."""
from __future__ import annotations

import configparser

try:
    import tomllib
except ImportError:  # Python < 3.11: every change to a pyproject.toml then counts as a config change
    tomllib = None

import fl_lib as fl

# Files that hold pytest or coverage settings next to other things, so only those parts are protected.
SHARED_CONFIG_FILES = ("pyproject.toml", "setup.cfg", "tox.ini")


def is_test(path: str, cfg: dict) -> bool:
    return fl.matches(path, cfg["test_globs"])


def test_problems(mb: str, root, cfg: dict, ns, added: dict[str, list[str]]) -> list[str]:
    problems, before, after = [], 0, 0
    for st, path, old in ns:
        src = old or path
        if is_test(src, cfg):
            if st in ("D", "R"):
                problems.append(f"test file {'deleted' if st == 'D' else 'renamed'}: {src}")
            before += fl.count_tests(fl.file_at(mb, src, root) or "")
        if is_test(path, cfg) and st != "D":
            after += fl.count_tests(fl.file_at("HEAD", path, root) or "")
    if after < before:
        problems.append(f"test count dropped: {before} -> {after}")
    for path, lines in added.items():
        for line in lines:
            if fl.SKIP_RE.search(line):
                problems.append(f"skip or suppression added in {path}: {line.strip()[:80]}")
    return problems


def runner_config(name: str, text: str | None) -> dict:
    """The pytest and coverage settings in the text of a pyproject.toml, setup.cfg or tox.ini.

    Keyed by `[tool.pytest]` / `[tool.coverage]`, or by the ini section. A file that cannot be parsed
    is returned whole, so any change to it counts.
    """
    if text is None:
        return {}
    try:
        if name == "pyproject.toml":
            tool = tomllib.loads(text).get("tool", {})
            return {f"[tool.{k}]": v for k, v in tool.items() if k.startswith(("pytest", "coverage"))}
        cp = configparser.RawConfigParser(strict=False)
        cp.optionxform = str
        cp.read_string(text)
        return {f"[{s}]": dict(cp.items(s)) for s in cp.sections()
                if s in ("pytest", "tool:pytest") or s.startswith("coverage:")}
    except Exception:
        return {"(unparseable)": text}


def runner_config_problems(mb: str, root, ns, allow) -> set[str]:
    found = set()
    for _st, path, old in ns:
        for p in {path, old} - {None}:
            name = p.rsplit("/", 1)[-1]
            if name not in SHARED_CONFIG_FILES or fl.matches(p, allow):
                continue
            before = runner_config(name, fl.file_at(mb, p, root))
            after = runner_config(name, fl.file_at("HEAD", p, root))
            changed = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
            if changed:
                found.add(f"protected test config changed: {p} {' '.join(changed)}")
    return found


def protected_problems(mb: str, root, cfg: dict, ns, allow) -> list[str]:
    found = runner_config_problems(mb, root, ns, allow)
    for _st, path, old in ns:
        for p in {path, old} - {None}:
            if fl.matches(p, allow):
                continue
            if fl.matches(p, cfg["always_protected"]) or (
                    fl.matches(p, cfg["protected_globs"]) and fl.exists_at(mb, p, root)):
                found.add(f"protected file changed: {p}")
    return sorted(found)


def size_problem(cfg: dict, stats, prefix: str = "") -> str | None:
    """prefix "" applies the per-ticket limits, "pr_" the per-PR limits."""
    max_lines, max_files = cfg[prefix + "max_lines"], cfg[prefix + "max_files"]
    counted = [(a, d, p) for a, d, p in stats if not fl.matches(p, cfg["size_exempt"])]
    lines = sum(a + d for a, d, _ in counted)
    if lines > max_lines or len(counted) > max_files:
        return f"too big: {lines} lines in {len(counted)} files (max {max_lines} lines, {max_files} files)"
    return None
