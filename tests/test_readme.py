"""The README names only real things, and every file the notices credit names its origin.

AC-66-2. A promise in the README names the tests that prove it as `tests/<file>.py::test_<name>`
in a code span; a command is any code span or code-block line with the word `wsync`, and a flag
may be named alone (`--repair`). AC-66-6 and ADR 0005: each file `THIRD_PARTY_NOTICES.md`
credits carries a header naming the project and the commit it was ported from.
"""

import argparse
import ast
import contextlib
import io
import re
import shlex
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from webhook_sync.cli.main import build_parser

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"

TEST_REF = re.compile(r"(tests/[\w/]+\.py)::(test_\w+)")
SRC_PATH = re.compile(r"src/webhook_sync/[\w/]+(?:\.py)?")
ORIGIN = re.compile(r"\[?([\w-]+)\]?(?:\([^)]+\))? at ([0-9a-f]{7})")


def code_spans(markdown: str) -> list[str]:
    """Every inline code span, and every line of every fenced code block but the diagrams."""
    found: list[str] = []
    fence: str | None = None
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            fence = None if fence is not None else line.strip().removeprefix("```")
        elif fence is None:
            found.extend(re.findall(r"`([^`\n]+)`", line))
        elif fence != "mermaid":
            found.append(line.strip())
    return found


def defined_tests(path: Path) -> set[str]:
    """The functions the file defines; none when there is no such file."""
    if not path.is_file():
        return set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def missing_tests(markdown: str) -> list[str]:
    """Each code span naming a file under `tests` that is not `tests/<file>.py::test_<name>`
    (a single colon, a space, a backslash, a helper's name), or whose test does not exist."""
    missing: list[str] = []
    for span in code_spans(markdown):
        if not re.search(r"\btests[/\\]", span):
            continue
        ref = TEST_REF.fullmatch(span)
        if ref is None or ref[2] not in defined_tests(ROOT / ref[1]):
            missing.append(span)
    return missing


def promises(markdown: str) -> list[str]:
    """Each numbered item of the text, with the lines under it."""
    items: list[str] = []
    inside = False
    for line in markdown.splitlines():
        if re.match(r"\d+\. ", line):
            items.append(line)
            inside = True
        elif inside and (not line.strip() or line[0].isspace()):
            items[-1] += "\n" + line
        else:
            inside = False
    return items


def flags(parser: argparse.ArgumentParser) -> set[str]:
    """Every option of the parser and of each command under it."""
    found: set[str] = set()
    for action in parser._actions:  # pyright: ignore[reportPrivateUsage]
        found.update(action.option_strings)
        choices: object = action.choices
        if isinstance(choices, dict):
            for sub in cast(Mapping[str, object], choices).values():
                if isinstance(sub, argparse.ArgumentParser):
                    found |= flags(sub)
    return found


def shown_commands(markdown: str) -> list[str]:
    """Each command the text shows: a code span or code-block line (split at `&&`) with the
    word `wsync`, or a flag named alone."""
    return [
        part.strip()
        for span in code_spans(markdown)
        for part in span.split("&&")
        if re.search(r"\bwsync\b", part) or part.strip().startswith("--")
    ]


def refused_commands(markdown: str) -> list[str]:
    """Each shown command that is not `wsync ...` or `uv run wsync ...` with arguments the
    parser accepts, and each flag named alone that no command has."""
    parser = build_parser()
    known = flags(parser)
    refused: list[str] = []
    for shown in shown_commands(markdown):
        try:
            words = shlex.split(shown)
        except ValueError:
            refused.append(shown)
            continue
        if shown.startswith("--"):
            accepted = words[0].split("=")[0] in known
        elif words[:3] == ["uv", "run", "wsync"]:
            accepted = _parses(parser, words[3:])
        elif words[:1] == ["wsync"]:
            accepted = _parses(parser, words[1:])
        else:
            accepted = False  # behind a prefix this check can't read: `$ wsync`, `VAR=1 wsync`
        if not accepted:
            refused.append(shown)
    return refused


def _parses(parser: argparse.ArgumentParser, args: list[str]) -> bool:
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            parser.parse_args(args)
    except SystemExit as stopped:
        return stopped.code == 0  # --help and --version print and leave with 0
    return True


def credited(notices: str) -> list[tuple[str, str, str]]:
    """Each file the notices credit, with the project and the commit it was ported from."""
    found: list[tuple[str, str, str]] = []
    for line in notices.splitlines():
        if not line.startswith("| `src/"):
            continue
        ours, origin = line.split("|")[1:3]
        named = ORIGIN.search(origin)
        assert named is not None, f"no '<project> at <commit>' in: {origin.strip()}"
        found.extend((path, named[1], named[2]) for path in SRC_PATH.findall(ours))
    return found


def test_every_test_the_readme_names_exists() -> None:
    assert missing_tests(README.read_text(encoding="utf-8")) == []


def test_every_promise_in_the_readme_names_a_test() -> None:
    found = promises(README.read_text(encoding="utf-8"))

    assert len(found) >= 5, "showcase.md lists five guarantees"
    assert [item.splitlines()[0] for item in found if not TEST_REF.search(item)] == []


def test_every_wsync_command_the_readme_shows_is_a_real_command() -> None:
    readme = README.read_text(encoding="utf-8")

    assert len(shown_commands(readme)) >= 5, "the quickstart alone shows doctor and serve"
    assert refused_commands(readme) == []


def test_every_source_file_the_readme_and_notices_name_exists() -> None:
    paths = {
        path
        for doc in (README, NOTICES)
        for path in SRC_PATH.findall(doc.read_text(encoding="utf-8"))
    }

    assert paths, "the notices name the files they credit"
    assert sorted(path for path in paths if not (ROOT / path).exists()) == []


def test_every_file_the_notices_credit_names_its_origin() -> None:
    files = credited(NOTICES.read_text(encoding="utf-8"))

    assert files, "the notices credit files"
    unnamed: list[str] = []
    for path, project, commit in files:
        header = ast.get_docstring(ast.parse((ROOT / path).read_text(encoding="utf-8"))) or ""
        if not all(word in header for word in (project, commit, NOTICES.name)):
            unnamed.append(f"{path}: {project} at {commit}")
    assert unnamed == []


def test_a_test_reference_that_is_misshapen_or_names_no_test_is_caught() -> None:
    real = "tests/test_readme.py::test_every_test_the_readme_names_exists"
    wrong = [
        "tests/test_readme.py::test_no_test_of_this_name",
        "tests/test_no_such_file.py::test_anything",
        "tests/test_readme.py::defined_tests",  # a helper, not a test
        "tests/test_readme.py:test_every_test_the_readme_names_exists",
        "tests/test_readme.py test_every_test_the_readme_names_exists",
        r"tests\test_readme.py::test_every_test_the_readme_names_exists",
    ]
    text = f"Proved by `{real}`.\n" + "".join(f"- `{ref}`\n" for ref in wrong)

    assert missing_tests(text) == wrong


def test_a_promise_with_no_test_is_seen() -> None:
    text = (
        "1. One row per payment.\n"
        "   - `tests/test_readme.py::test_a_promise_with_no_test_is_seen`\n"
        "\n"
        "2. Nothing is lost.\n"
        "   - tests/test_readme.py:test_misshapen\n"
        "\n"
        "A paragraph with `tests/test_readme.py::test_out_of_any_promise`.\n"
    )

    found = promises(text)

    assert [item.splitlines()[0] for item in found] == [
        "1. One row per payment.",
        "2. Nothing is lost.",
    ]
    assert [bool(TEST_REF.search(item)) for item in found] == [True, False]


def test_a_command_the_parser_refuses_or_the_check_cannot_read_is_caught() -> None:
    wrong = [
        "uv run wsync demo rewind",
        "wsync doctor --url",
        "$ wsync serve",
        "uv run --extra demo wsync serve",
        "WSYNC_CRASH_AT=mid_slice wsync serve",
        "--no-such-flag",
    ]
    text = (
        "Run `uv run wsync serve`, then `wsync verify --full` with `--repair` or `--orders N`.\n"
        "```\n" + "\n".join(wrong[:3]) + "\ncd here && uv run wsync doctor\n```\n"
        "```mermaid\n"
        'SVC["wsync serve: one process"]\n'
        "```\n" + "".join(f"- `{command}`\n" for command in wrong[3:])
    )

    assert refused_commands(text) == wrong
