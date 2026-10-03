"""Mutation testing for the ticket gate: exits 1 when the mutation score of the given files is under --min.

    uv run python scripts/claude/mutation.py src/webhook_sync/queue/retry.py [more.py ...] [--min 70]

Standard library only, and the same on Windows and Linux (mutmut 3 needs os.fork). Nothing is written
to the repo: the source tree is copied to a temp dir, which goes first on PYTHONPATH.

1. Mutants: each file is parsed with ast and one small change made at a time (comparison, arithmetic
   and boolean operators, `not`, constants, `return x` -> `return None`, break <-> continue).
   Docstrings and other bare strings, annotations, the constants in data tables (literals of 8+
   items), `if TYPE_CHECKING:` and lines marked `# pragma: no mutate` are left alone.
2. Baseline: the test files that import a mutated module (directly or through other modules) run
   once, in parallel shards, with a sys.monitoring tracer recording which tests run which lines,
   in the pytest process and in the Python processes a test starts (`wsync serve`).
3. Each mutant runs only the tests that ran its lines (fastest first, -x); a module-level mutant also
   gets the tests that ran the modules importing it. The unmutated file must pass the same way, or
   the run is an error. A mutant is killed when the tests fail, error, or one overruns 3x its
   baseline time + 15s; it survives when they pass or when no test runs its lines.
Paths outside --src or not ending in .py are ignored; with nothing to mutate it exits 0.
Exit codes: 0 score >= --min, 1 score below it, 2 the baseline tests failed or a usage error.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN = r'''
# Loaded into pytest with -p. MUTATION_TRACE_OUT: record which tests run which lines of
# MUTATION_TARGETS (baseline). MUTATION_BUDGETS: flag a test that overruns its time budget (mutants).
import json, os, sys, threading, time
import pytest

_current = None
_started = 0.0

if os.environ.get("MUTATION_TRACE_OUT"):
    _mon = sys.monitoring
    _TARGETS = {os.path.normcase(os.path.realpath(p)) for p in os.environ["MUTATION_TARGETS"].split(os.pathsep)}
    _hits, _durations, _names = {}, {}, {}

    def _line(code, line):
        name = _names.get(code.co_filename)
        if name is None:
            real = os.path.normcase(os.path.realpath(code.co_filename))
            name = _names[code.co_filename] = real if real in _TARGETS else ""
        if name and _current is not None:
            _hits.setdefault(_current, {}).setdefault(name, set()).add(line)
        return _mon.DISABLE

    _TOOL = next(i for i in (_mon.COVERAGE_ID, 3, 4) if _mon.get_tool(i) is None)
    _mon.use_tool_id(_TOOL, "mutation-trace")
    _mon.register_callback(_TOOL, _mon.events.LINE, _line)
    _mon.set_events(_TOOL, _mon.events.LINE)

    def pytest_sessionfinish(session, exitstatus):
        hits = {t: {f: sorted(ls) for f, ls in fs.items()} for t, fs in _hits.items()}
        with open(os.environ["MUTATION_TRACE_OUT"], "w", encoding="utf-8") as fh:
            json.dump({"hits": hits, "durations": _durations}, fh)

if os.environ.get("MUTATION_BUDGETS"):
    with open(os.environ["MUTATION_BUDGETS"], encoding="utf-8") as fh:
        _budgets = json.load(fh)

    def _watch():
        while True:
            time.sleep(0.2)
            test = _current
            if test is not None and time.monotonic() - _started > _budgets.get(test, 10.0):
                with open(os.environ["MUTATION_TIMEOUT_FLAG"], "w", encoding="utf-8") as fh:
                    fh.write(test)
                return

    threading.Thread(target=_watch, daemon=True).start()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(item, nextitem):
    global _current, _started
    _started = time.monotonic()
    _current = item.nodeid
    if os.environ.get("MUTATION_TRACE_OUT"):
        _mon.restart_events()
        os.environ["MUTATION_CURRENT_TEST"] = item.nodeid  # for sitecustomize in child processes
    try:
        return (yield)
    finally:
        if os.environ.get("MUTATION_TRACE_OUT"):
            _durations[item.nodeid] = time.monotonic() - _started
            os.environ.pop("MUTATION_CURRENT_TEST", None)
        _current = None
'''

# Imported by every Python process started with the plugin dir on PYTHONPATH. In a child process a
# baseline test starts (`wsync serve`), it records the target lines that process runs, under that
# test, a line at a time: the child may be killed rather than exit.
SITECUSTOMIZE = r'''
import os, sys

if os.environ.get("MUTATION_TRACE_OUT") and os.environ.get("MUTATION_CURRENT_TEST"):
    _mon = sys.monitoring
    _TARGETS = {os.path.normcase(os.path.realpath(p)) for p in os.environ["MUTATION_TARGETS"].split(os.pathsep)}
    _names = {}
    _fh = open(f"{os.environ['MUTATION_TRACE_OUT']}.{os.getpid()}.child", "w", encoding="utf-8", buffering=1)
    _fh.write(os.environ["MUTATION_CURRENT_TEST"] + "\n")

    def _line(code, line):
        name = _names.get(code.co_filename)
        if name is None:
            real = os.path.normcase(os.path.realpath(code.co_filename))
            name = _names[code.co_filename] = real if real in _TARGETS else ""
        if name:
            _fh.write(f"{name}\t{line}\n")
        return _mon.DISABLE

    _TOOL = next(i for i in (_mon.COVERAGE_ID, 3, 4) if _mon.get_tool(i) is None)
    _mon.use_tool_id(_TOOL, "mutation-trace")
    _mon.register_callback(_TOOL, _mon.events.LINE, _line)
    _mon.set_events(_TOOL, _mon.events.LINE)
'''

CMP_SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE,
            ast.LtE: ast.Gt, ast.Is: ast.IsNot, ast.IsNot: ast.Is, ast.In: ast.NotIn, ast.NotIn: ast.In}
BIN_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult, ast.FloorDiv: ast.Div,
            ast.Mod: ast.FloorDiv, ast.Pow: ast.Mult, ast.BitAnd: ast.BitOr, ast.BitOr: ast.BitAnd,
            ast.BitXor: ast.BitAnd, ast.LShift: ast.RShift, ast.RShift: ast.LShift}
SYMBOL = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.GtE: ">=", ast.Gt: ">", ast.LtE: "<=",
          ast.Is: "is", ast.IsNot: "is not", ast.In: "in", ast.NotIn: "not in", ast.Add: "+",
          ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**",
          ast.BitAnd: "&", ast.BitOr: "|", ast.BitXor: "^", ast.LShift: "<<", ast.RShift: ">>",
          ast.And: "and", ast.Or: "or"}
TYPING_CALLS = {"cast", "TypeVar", "NewType", "ParamSpec", "TypeVarTuple"}
TYPING_SUBSCRIPTS = {"Literal", "Annotated"}
DUNDER_LISTS = {"__all__", "__slots__", "__match_args__"}
CHUNK_SECONDS = 20.0  # baseline test time per mutant job; longer lists run in parallel parts
TABLE_SIZE = 8  # a list, tuple or set literal of this many items, or a dict of this many pairs


@dataclass
class Mutant:
    path: str  # repo-relative, posix
    line: int
    start_line: int  # the tests that ran any line from here to end_line run the mutant
    end_line: int
    module_level: bool
    desc: str
    source: str
    status: str = ""
    tests: list[str] = field(default_factory=list)
    output: str = ""
    seconds: float = 0.0


def _is_docstring(stmt: ast.stmt) -> bool:
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)


def _name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _skip_if(node: ast.If) -> bool:
    t = node.test
    if _name(t) == "TYPE_CHECKING":
        return True
    return isinstance(t, ast.Compare) and _name(t.left) == "__name__"


def mutants_for(rel: str, text: str) -> list[Mutant]:
    tree = ast.parse(text)
    no_mutate = {i for i, ln in enumerate(text.splitlines(), 1) if "pragma: no mutate" in ln}
    out: list[Mutant] = []
    stmt_line = [0]  # the first line of the statement being visited

    def emit(node: ast.AST, desc: str, apply, revert, in_body: bool) -> None:
        line = getattr(node, "lineno", 0)
        if line in no_mutate:
            return
        apply()
        try:
            src = ast.unparse(tree)
        finally:
            revert()
        end = getattr(node, "end_lineno", line) or line
        out.append(Mutant(rel, line, min(line, stmt_line[0] or line), end, not in_body, desc, src))

    def set_attr(obj, attr, value):
        old = getattr(obj, attr)
        return (lambda: setattr(obj, attr, value)), (lambda: setattr(obj, attr, old))

    def set_item(lst, i, value):
        old = lst[i]

        def apply():
            lst[i] = value

        def revert():
            lst[i] = old
        return apply, revert

    def mutate(node: ast.AST, in_body: bool) -> None:
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                new = CMP_SWAP.get(type(op))
                if new:
                    emit(node, f"{SYMBOL[type(op)]} -> {SYMBOL[new]}", *set_item(node.ops, i, new()), in_body)
        elif isinstance(node, (ast.BinOp, ast.AugAssign)):
            new = BIN_SWAP.get(type(node.op))
            if new:
                emit(node, f"{SYMBOL[type(node.op)]} -> {SYMBOL[new]}", *set_attr(node, "op", new()), in_body)
        elif isinstance(node, ast.BoolOp):
            new = ast.Or if isinstance(node.op, ast.And) else ast.And
            emit(node, f"{SYMBOL[type(node.op)]} -> {SYMBOL[new]}", *set_attr(node, "op", new()), in_body)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            emit(node, "not x -> x", *set_attr(node, "operand", ast.UnaryOp(ast.Not(), node.operand)), in_body)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            emit(node, "-x -> x", *set_attr(node, "op", ast.UAdd()), in_body)
        elif isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                emit(node, f"{v} -> {not v}", *set_attr(node, "value", not v), in_body)
            elif isinstance(v, (int, float)):
                emit(node, f"{v!r} -> {v + 1!r}", *set_attr(node, "value", v + 1), in_body)
            elif isinstance(v, str):
                emit(node, f"{v[:20]!r} -> 'XX..XX'", *set_attr(node, "value", f"XX{v}XX"), in_body)
        elif isinstance(node, ast.Return) and node.value is not None:
            if not isinstance(node.value, ast.Constant):  # a constant is mutated as a constant
                emit(node, "return x -> return None", *set_attr(node, "value", ast.Constant(None)), in_body)

    def visit(node: ast.AST, in_body: bool) -> None:
        if isinstance(node, ast.stmt):
            outer, stmt_line[0] = stmt_line[0], node.lineno
            try:
                visit_node(node, in_body)
            finally:
                stmt_line[0] = outer
        else:
            visit_node(node, in_body)

    def visit_node(node: ast.AST, in_body: bool) -> None:
        if isinstance(node, ast.JoinedStr):
            for v in node.values:
                if isinstance(v, ast.FormattedValue):
                    visit(v.value, in_body)
            return
        if isinstance(node, ast.Subscript) and _name(node.value) in TYPING_SUBSCRIPTS:
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_name(t) in DUNDER_LISTS for t in targets):
                return
        if isinstance(node, getattr(ast, "TypeAlias", ())):
            return
        if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            items = node.keys + node.values if isinstance(node, ast.Dict) else node.elts
            if len(items) >= 2 * TABLE_SIZE if isinstance(node, ast.Dict) else len(items) >= TABLE_SIZE:
                for item in items:  # a data table: its entries are data, not logic
                    if item is not None and not isinstance(item, ast.Constant):
                        visit(item, in_body)
                return
        mutate(node, in_body)
        typing_call = isinstance(node, ast.Call) and _name(node.func) in TYPING_CALLS
        for fname, value in ast.iter_fields(node):
            if fname in ("annotation", "returns", "type_comment", "type_params"):
                continue
            body = in_body or (fname == "body" and isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))
            if isinstance(value, ast.AST):
                visit(value, body)
                continue
            if not isinstance(value, list):
                continue
            for i, child in enumerate(value):
                if not isinstance(child, ast.AST):
                    continue
                if typing_call and fname == "args" and i == 0:
                    continue  # cast("T", x), TypeVar("T"): the name cannot change behaviour
                if _is_docstring(child):
                    continue  # docstrings, and the strings that document an attribute
                if isinstance(child, ast.If) and _skip_if(child):
                    for s in child.orelse:
                        visit(s, body)
                    continue
                if isinstance(child, (ast.Break, ast.Continue)):
                    brk = isinstance(child, ast.Break)
                    emit(child, "break -> continue" if brk else "continue -> break",
                         *set_item(value, i, ast.Continue() if brk else ast.Break()), body)
                    continue
                visit(child, body)

    visit(tree, False)
    return out


def module_name(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def imports_of(path: Path, mod: str, is_pkg: bool) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                pkg = mod.split(".") if is_pkg else mod.split(".")[:-1]
                pkg = pkg[: len(pkg) - (node.level - 1)]
                base = ".".join([*pkg, base] if base else pkg)
            out.add(base)
            out.update(f"{base}.{a.name}" for a in node.names)
    return out


def candidate_tests(root: Path, src: Path, tests: Path, targets: set[str]) -> list[str]:
    """Test files that import a target module, directly or through other modules in src or tests."""
    graph: dict[str, set[str]] = {}
    files: dict[str, Path] = {}
    for base in (src, tests):
        for p in base.rglob("*.py"):
            if "__pycache__" in p.parts:
                continue
            rel = p.relative_to(base)
            mod = module_name(rel)
            files[mod] = p
            graph[mod] = imports_of(p, mod, rel.name == "__init__.py")
    reached = set(targets)
    changed = True
    while changed:
        changed = False
        for mod, deps in graph.items():
            if mod not in reached and deps & reached:
                reached.add(mod)
                changed = True
    # A conftest.py that reaches a target hands it, through its fixtures, to every test below it.
    fixture_dirs = [files[m].parent for m in reached if files.get(m) and files[m].name == "conftest.py"]
    out = []
    for mod, p in files.items():
        if p.is_relative_to(tests) and p.name.startswith("test_") and (
                mod in reached or any(p.is_relative_to(d) for d in fixture_dirs)):
            out.append(p.relative_to(root).as_posix())
    # The modules in src that import a target (through a package __init__ too): a module-level
    # mutant (a constant, a table) shows in what they do.
    users = {m for m, deps in graph.items() if m not in targets and deps & targets}
    users |= {m for m, deps in graph.items() if m not in targets and deps & {u for u in users
                                                                             if files[u].name == "__init__.py"}}
    importers = sorted(files[m].relative_to(root).as_posix() for m in users if files[m].is_relative_to(src))
    return sorted(out), importers


def _ticket_base(root: Path) -> str | None:
    try:
        return json.loads((root / ".fl-ticket.json").read_text(encoding="utf-8")).get("base") or None
    except (OSError, ValueError):
        return None


def changed_lines(root: Path, base: str, rels: list[str]) -> dict[str, set[int] | None]:
    """Per file, the lines added or changed since the merge-base with `base`; None means every line
    (the file is new, or the merge-base cannot be found)."""
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8")
    mb = git("merge-base", base, "HEAD")
    if mb.returncode != 0:
        print(f"mutation: no merge-base with {base}; mutating whole files")
        return {r: None for r in rels}
    out: dict[str, set[int] | None] = {}
    for rel in rels:
        if git("cat-file", "-e", f"{mb.stdout.strip()}:{rel}").returncode != 0:
            out[rel] = None
            continue
        lines: set[int] = set()
        for ln in git("diff", "-U0", mb.stdout.strip(), "--", rel).stdout.splitlines():
            if ln.startswith("@@"):
                new = ln.split("+", 1)[1].split(" ", 1)[0]
                start, _, count = new.partition(",")
                lines.update(range(int(start), int(start) + (int(count) if count else 1)))
        out[rel] = lines
    return out


def kill_tree(p: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    else:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run(cmd: list[str], cwd: Path, env: dict, timeout: float | None, log: Path,
        flag: Path | None = None) -> tuple[int | None, str]:
    """(exit code, output tail); exit code None when it overran `timeout` or a test raised `flag`."""
    kw = {} if os.name == "nt" else {"start_new_session": True}
    with log.open("w", encoding="utf-8") as out:
        p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT, **kw)
        deadline = None if timeout is None else time.monotonic() + timeout
        code: int | None = None
        while True:
            try:
                code = p.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if (flag and flag.exists()) or (deadline and time.monotonic() > deadline):
                    kill_tree(p)
                    p.wait()
                    break
    text = log.read_text(encoding="utf-8", errors="replace")
    return code, "\n".join(text.strip().splitlines()[-30:])


def pytest_cmd(argsfile: Path, *extra: str) -> list[str]:
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--no-header", *extra, f"@{argsfile}"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="*")
    ap.add_argument("--min", type=float, default=70.0, help="lowest passing score, in percent (default 70)")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2),
                    help="parallel pytest runs (default: half the cores; more starves timing-bound tests)")
    ap.add_argument("--src", default="src")
    ap.add_argument("--tests", default="tests")
    ap.add_argument("--root", default=".")
    ap.add_argument("--base", help="mutate only lines changed since the merge-base with this ref "
                    "(default: the \"base\" in .fl-ticket.json; with neither, whole files)")
    a = ap.parse_args(argv)

    root = Path(a.root).resolve()
    src, tests = (root / a.src).resolve(), (root / a.tests).resolve()
    rels: list[str] = []
    for f in a.files:
        p = (root / f).resolve()
        if p.suffix == ".py" and p.is_file() and p.is_relative_to(src):
            rels.append(p.relative_to(root).as_posix())
        else:
            print(f"skipped (not a .py file under {a.src}/): {f}")
    rels = sorted(set(rels))

    mutants: list[Mutant] = []
    for rel in rels:
        text = (root / rel).read_text(encoding="utf-8")
        mutants += mutants_for(rel, text)
    base = a.base or _ticket_base(root)
    if base and mutants:
        changed = changed_lines(root, base, rels)
        before = len(mutants)
        mutants = [m for m in mutants
                   if changed.get(m.path) is None or changed[m.path] & set(range(m.line, m.end_line + 1))]
        print(f"mutation: {len(mutants)} of {before} mutants are on lines changed since {base}")
    if not mutants:
        print("mutation: nothing to mutate")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="wsync-mutation-"))
    try:
        return _run(a, root, src, tests, rels, mutants, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run(a, root: Path, src: Path, tests: Path, rels: list[str], mutants: list[Mutant], tmp: Path) -> int:
    started = time.monotonic()
    targets = {module_name((root / r).relative_to(src)) for r in rels}
    test_files, importers = candidate_tests(root, src, tests, targets)
    print(f"mutation: {len(mutants)} mutants in {len(rels)} file(s); {len(test_files)} test file(s) import them")

    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    base_src = tmp / "base" / "src"
    shutil.copytree(src, base_src, ignore=ignore)
    plugin_dir = tmp / "plugin"
    plugin_dir.mkdir()
    (plugin_dir / "_mutation_plugin.py").write_text(PLUGIN, encoding="utf-8")
    (plugin_dir / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    copy_of = {os.path.normcase(os.path.realpath(base_src / (root / r).relative_to(src))): r
               for r in [*rels, *importers]}
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}

    hits: dict[str, dict[str, set[int]]] = {}
    durations: dict[str, float] = {}
    if test_files:
        shards = [test_files[i:: max(1, min(a.jobs, len(test_files)))] for i in range(min(a.jobs, len(test_files)))]

        def baseline(i: int) -> tuple[int | None, str, Path]:
            argsfile, out = tmp / f"baseline-{i}.args", tmp / f"baseline-{i}.json"
            argsfile.write_text("\n".join(shards[i]) + "\n", encoding="utf-8")
            e = {**env, "PYTHONPATH": os.pathsep.join([str(base_src), str(plugin_dir)]),
                 "MUTATION_TARGETS": os.pathsep.join(copy_of), "MUTATION_TRACE_OUT": str(out)}
            for _ in range(2):  # once more on failure: timing-sensitive tests can flake under load
                code, text = run(pytest_cmd(argsfile, "-p", "_mutation_plugin"), root, e, None,
                                 tmp / f"baseline-{i}.log")
                if code in (0, 5):
                    break
            return code, text, out

        with ThreadPoolExecutor(len(shards)) as pool:
            for code, text, out in pool.map(baseline, range(len(shards))):
                if code not in (0, 5):
                    print(text)
                    print("mutation: ERROR the tests fail before any mutation; fix them first")
                    return 2
                data = json.loads(out.read_text(encoding="utf-8"))
                durations.update(data["durations"])
                for t, fs in data["hits"].items():
                    hits[t] = {copy_of[f]: set(ls) for f, ls in fs.items() if f in copy_of}
        for child in tmp.glob("baseline-*.json.*.child"):
            test, *rows = child.read_text(encoding="utf-8", errors="replace").splitlines()
            for row in rows:
                f, _, ln = row.rpartition("\t")
                if f in copy_of and ln.isdigit():
                    hits.setdefault(test, {}).setdefault(copy_of[f], set()).add(int(ln))
    print(f"mutation: baseline {len(durations)} tests in {time.monotonic() - started:.0f}s")

    by_file: dict[str, set[str]] = {}
    by_line: dict[tuple[str, int], set[str]] = {}
    for t, fs in hits.items():
        for f, ls in fs.items():
            by_file.setdefault(f, set()).add(t)
            for ln in ls:
                by_line.setdefault((f, ln), set()).add(t)
    for m in mutants:
        if m.module_level:
            chosen = set().union(*(by_file.get(f, set()) for f in [m.path, *importers]))
        else:
            chosen = set().union(*(by_line.get((m.path, ln), set()) for ln in range(m.start_line, m.end_line + 1)))
        m.tests = sorted(chosen, key=lambda t: (durations.get(t, 0.0), t))
        if not m.tests:
            m.status = "no tests"

    to_run = [m for m in mutants if m.tests]
    # A null mutant per file (its own source, through ast.unparse) must pass, or every kill is suspect.
    nulls = [Mutant(r, 0, 0, 0, True, "unchanged", ast.unparse(ast.parse((root / r).read_text(encoding="utf-8"))),
                    tests=sorted(by_file[r], key=lambda t: (durations.get(t, 0.0), t)))
             for r in rels if by_file.get(r)]

    def chunks(tests: list[str]) -> list[list[str]]:
        """A long test list split into parallel jobs of about CHUNK_SECONDS each, fastest tests first."""
        total = sum(durations.get(t, 1.0) for t in tests)
        k = max(1, min(a.jobs, len(tests), math.ceil(total / CHUNK_SECONDS)))
        return [tests[i::k] for i in range(k)]

    jobs = [(m, c) for m in nulls + to_run for c in chunks(m.tests)]
    outcomes: dict[int, list[str]] = {id(m): [] for m in nulls + to_run}
    n_workers = max(1, min(a.jobs, len(jobs)))
    workers: queue.Queue[Path] = queue.Queue()
    for i in range(n_workers):
        w = tmp / f"w{i}" / "src"
        shutil.copytree(base_src, w, ignore=ignore)
        workers.put(w)
    lock = threading.Lock()
    counter = [0]
    # A test that overruns three times its baseline time plus 15s times the mutant out.
    budgets = tmp / "budgets.json"
    budgets.write_text(json.dumps({t: 3 * d + 15 for t, d in durations.items()}), encoding="utf-8")

    def attempt(job: tuple[Mutant, list[str]]) -> None:
        m, tests = job
        with lock:
            if any(o in ("killed", "timeout") for o in outcomes[id(m)]):
                return  # another part of its tests killed it already
            counter[0] += 1
            n = counter[0]
        w = workers.get()
        target = w / (root / m.path).relative_to(src)
        original = target.read_bytes()
        try:
            target.write_text(m.source, encoding="utf-8")
            argsfile, flag = tmp / f"mutant-{n}.args", tmp / f"mutant-{n}.timeout"
            argsfile.write_text("\n".join(tests) + "\n", encoding="utf-8")
            limit = 60 + 3 * sum(durations.get(t, 1.0) for t in tests)
            e = {**env, "PYTHONPATH": os.pathsep.join([str(w), str(plugin_dir)]), "PYTHONDONTWRITEBYTECODE": "1",
                 "MUTATION_BUDGETS": str(budgets), "MUTATION_TIMEOUT_FLAG": str(flag)}
            t0 = time.monotonic()
            code, output = run(pytest_cmd(argsfile, "-x", "-p", "_mutation_plugin"), root, e, limit,
                               tmp / f"mutant-{n}.log", flag)
            outcome = ("timeout" if code is None else "survived" if code == 0
                       else "no tests" if code == 5 else "killed")
            with lock:
                outcomes[id(m)].append(outcome)
                m.seconds = max(m.seconds, time.monotonic() - t0)
                if outcome != "survived":
                    m.output = output
                if outcome == "timeout" and flag.exists():
                    m.output += f"\nmutation: timed out in {flag.read_text(encoding='utf-8')}"
        finally:
            target.write_bytes(original)
            workers.put(w)

    with ThreadPoolExecutor(n_workers) as pool:
        list(pool.map(attempt, jobs))
    for m in nulls + to_run:
        got = outcomes[id(m)]
        m.status = next((s for s in ("killed", "timeout", "no tests") if s in got), "survived")
    for n in nulls:
        if n.status != "survived":
            print(n.output)
            print(f"mutation: ERROR {n.path} unchanged fails its tests under this harness ({n.status})")
            return 2

    survivors = [m for m in mutants if m.status in ("survived", "no tests")]
    if to_run:
        slow = max(to_run, key=lambda m: m.seconds)
        print(f"mutation: ran {len(to_run)} mutants, {sum(m.status == 'timeout' for m in to_run)} timed out; "
              f"slowest {slow.seconds:.0f}s ({len(slow.tests)} tests, {slow.status}: {slow.path}:{slow.line})")
    for m in survivors[:25]:
        print(f"  {m.status.upper():9} {m.path}:{m.line}  {m.desc}")
    if len(survivors) > 25:
        print(f"  ... and {len(survivors) - 25} more")
    total_killed = 0
    for rel in rels:
        ms = [m for m in mutants if m.path == rel]
        killed = sum(m.status in ("killed", "timeout") for m in ms)
        total_killed += killed
        print(f"mutation: {rel}: {killed}/{len(ms)} killed ({100 * killed / len(ms):.1f}%)")
    score = 100 * total_killed / len(mutants)
    ok = score >= a.min
    print(f"mutation: score {score:.1f}% ({total_killed}/{len(mutants)} killed) in "
          f"{time.monotonic() - started:.0f}s; minimum {a.min:g}%: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
