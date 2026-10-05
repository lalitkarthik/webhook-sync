"""The README names only real things: each test it cites exists, each `wsync` command parses.

AC-66-2. A guarantee in the README names the test that proves it as `tests/<file>.py::<test>`;
a command is any code span or code-block line starting `wsync` or `uv run wsync`. Paths under
`src/` named by the README or the third-party notices must exist too.
"""

import ast
import re
import shlex
from pathlib import Path

from webhook_sync.cli.main import build_parser

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
NOTICES = ROOT / "THIRD_PARTY_NOTICES.md"

TEST_REF = re.compile(r"(tests/[\w/]+\.py)::(\w+)")
SRC_PATH = re.compile(r"src/webhook_sync/[\w/]+(?:\.py)?")


def code_lines(markdown: str) -> list[str]:
    """Every inline code span, and every line of every fenced code block."""
    found: list[str] = []
    fenced = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            found.append(line.strip())
        else:
            found.extend(re.findall(r"`([^`\n]+)`", line))
    return found


def wsync_commands(markdown: str) -> list[list[str]]:
    """The arguments of each `wsync` command the text shows, without `uv run wsync`."""
    commands: list[list[str]] = []
    for line in code_lines(markdown):
        for part in line.split("&&"):
            if "wsync" not in part:
                continue
            words = shlex.split(part.strip())
            if words[:3] == ["uv", "run", "wsync"]:
                commands.append(words[3:])
            elif words[:1] == ["wsync"]:
                commands.append(words[1:])
    return commands


def defined_tests(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def test_every_test_the_readme_names_exists() -> None:
    refs = TEST_REF.findall(README.read_text(encoding="utf-8"))

    assert len(refs) >= 5, "the guarantees name their tests as tests/<file>.py::<test>"
    missing = [
        f"{file}::{name}"
        for file, name in refs
        if not (ROOT / file).is_file() or name not in defined_tests(ROOT / file)
    ]
    assert missing == []


def test_every_wsync_command_the_readme_shows_is_a_real_command() -> None:
    commands = wsync_commands(README.read_text(encoding="utf-8"))

    assert len(commands) >= 5, "the quickstart alone shows wsync doctor and wsync serve"
    parser = build_parser()
    refused: list[str] = []
    for args in commands:
        try:
            parser.parse_args(args)
        except SystemExit:
            refused.append(shlex.join(["wsync", *args]))
    assert refused == []


def test_every_source_file_the_readme_and_notices_name_exists() -> None:
    paths = {
        path
        for doc in (README, NOTICES)
        for path in SRC_PATH.findall(doc.read_text(encoding="utf-8"))
    }

    assert paths, "the notices name the files they credit"
    assert sorted(path for path in paths if not (ROOT / path).exists()) == []


def test_a_command_the_parser_refuses_is_caught() -> None:
    commands = wsync_commands("Run `uv run wsync demo rewind` and\n```\nwsync doctor --url\n```\n")

    assert commands == [["demo", "rewind"], ["doctor", "--url"]]
    parser = build_parser()
    for args in commands:
        try:
            parser.parse_args(args)
        except SystemExit:
            continue
        raise AssertionError(f"wsync {shlex.join(args)} parsed")
