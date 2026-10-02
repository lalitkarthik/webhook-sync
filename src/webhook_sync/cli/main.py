"""The `wsync` command line."""

import argparse
from collections.abc import Sequence
from importlib.metadata import version


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wsync",
        description="Receive payment events, keep one ledger row per payment, mirror it to a sheet.",
    )
    parser.add_argument("--version", action="version", version=f"wsync {version('webhook-sync')}")
    parser.add_subparsers(dest="command", metavar="<command>")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
