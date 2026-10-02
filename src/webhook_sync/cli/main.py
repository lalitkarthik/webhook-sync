"""The `wsync` command line."""

import argparse
from collections.abc import Callable, Sequence
from importlib.metadata import version

from webhook_sync.cli import serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wsync",
        description="Keep one ledger row per payment and mirror it to a Google Sheet.",
    )
    parser.add_argument("--version", action="version", version=f"wsync {version('webhook-sync')}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    serve.add_parser(subparsers)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] | None = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)
