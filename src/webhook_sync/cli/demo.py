"""`wsync demo <scene>`: the scenes of the video, run on demand (showcase.md; Q105, Q120, Q181).

Every scene refuses, before it touches anything, unless the Razorpay key is a test-mode key and
the configured spreadsheet is on `[demo] spreadsheet_ids`, so a mistyped id can never empty a
real sheet (F36). Each prints what it is doing and what it waits for.
"""

import argparse
import asyncio
import functools
import re
import sys
from collections.abc import Callable
from pathlib import Path

import httpx

from webhook_sync.cli.admin import EXIT_REFUSED, CommandFailed, verify_command
from webhook_sync.cli.doctor import sheet_failure
from webhook_sync.cli.serve import prepare_database
from webhook_sync.config import Config, ConfigError, load_config, load_settings
from webhook_sync.db.files import database_path
from webhook_sync.db.lock import lock_if_free
from webhook_sync.receiver.demo import check_demo_switch
from webhook_sync.sinks.sheets import reset_tabs
from webhook_sync.sinks.sheets_client import ServiceAccountTokens, SheetsClient

DEFAULT_CONFIG = Path("webhook-sync.toml")
DATABASE_SUFFIXES = ("", "-wal", "-shm", "-journal")
"""The database file and the files SQLite keeps beside it."""

say = functools.partial(print, flush=True)

type Scene = Callable[[argparse.Namespace, Config], int]


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser(
        "demo", help="run a scene of the demo against Razorpay test mode and the demo sheet"
    )
    scenes = parser.add_subparsers(dest="scene", metavar="<scene>", required=True)
    for name, says, scene in (
        (
            "reset",
            "empty the database and the demo sheet's tabs; set the demo retry profile",
            reset,
        ),
        ("outage", "pay test orders while the receiver is stopped, then sweep them in", outage),
        ("verify", "run a full verify and print its line", verify),
    ):
        scene_parser = scenes.add_parser(name, help=says)
        scene_parser.add_argument(
            "--config",
            type=Path,
            default=DEFAULT_CONFIG,
            help="the config file (default: ./webhook-sync.toml)",
        )
        scene_parser.set_defaults(handler=_guarded(name, scene))


def _guarded(name: str, scene: Scene) -> Callable[[argparse.Namespace], int]:
    """`scene`, run only with a test-mode key and an allow-listed sheet (Q120)."""

    def run(args: argparse.Namespace) -> int:
        try:
            try:
                config = load_config(args.config)
                check_demo_switch(config, "wsync demo")
            except ConfigError as error:
                raise CommandFailed(str(error), EXIT_REFUSED) from None
            return scene(args, config)
        except CommandFailed as failed:
            print(f"wsync demo {name}: {failed}", file=sys.stderr)
            return failed.exit_code

    return run


def reset(args: argparse.Namespace, config: Config) -> int:
    """Empty the database and our tabs of the demo sheet, and set the demo retry profile. It
    holds the lock throughout, so it refuses while a server runs: a server keeps its retry
    profile until it restarts, and its database is not emptied under it (ADR 0007)."""
    data_dir = config.service.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    with lock_if_free(data_dir) as free:
        if not free:
            raise CommandFailed(
                "a server is running: stop wsync serve first; reset empties its database",
                EXIT_REFUSED,
            )
        database = database_path(data_dir)
        say(f"emptying the database {database}")
        for suffix in DATABASE_SUFFIXES:
            database.with_name(database.name + suffix).unlink(missing_ok=True)
        asyncio.run(prepare_database(config))
        say("database emptied: migrated, with no events, jobs or payments")
        say("emptying the demo sheet's Payments, Daily summary and Sync status tabs")
        asyncio.run(_reset_sheet(config))
        say(
            "Payments, Daily summary and Sync status emptied, each with its header; "
            "the sheet's other tabs are untouched"
        )
        set_demo_retry_profile(args.config)
        say(
            f'retry_profile = "demo" set in [service] of {args.config.name}: '
            "failed jobs retry after 1, 2, 4, 8 and 16 s from the next wsync serve"
        )
    return 0


async def _reset_sheet(config: Config) -> None:
    account = f"the service account {config.google_secrets.service_account.get('client_email')}"
    tokens = ServiceAccountTokens(config.google_secrets.service_account)
    async with httpx.AsyncClient() as http:
        failure = await reset_tabs(SheetsClient(http, config.sheets.spreadsheet_id, tokens), config)
    if failure is not None:
        raise CommandFailed(sheet_failure(failure, account))


_SERVICE_TABLE = re.compile(r"\[\s*service\s*\]")
_RETRY_PROFILE = re.compile(r"retry_profile\s*=")
_TABLE = re.compile(r"\s*\[")


def set_demo_retry_profile(path: Path) -> None:
    """`retry_profile = "demo"` in the config file's `[service]` table, every other line kept
    as it was; the file is read back to prove it."""
    lines = path.read_text(encoding="utf-8", newline="").splitlines(keepends=True)
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    setting = f'retry_profile = "demo"{newline}'
    in_service, service_at, done = False, None, False
    for at, line in enumerate(lines):
        if _TABLE.match(line):
            in_service = bool(_SERVICE_TABLE.match(line.strip().split("#")[0].strip()))
            if in_service:
                service_at = at
        elif in_service and _RETRY_PROFILE.match(line.strip()):
            lines[at] = setting
            done = True
            break
    if not done:
        if service_at is None:
            lines[:0] = [f"[service]{newline}", setting, newline]
        else:
            lines.insert(service_at + 1, setting)
    path.write_text("".join(lines), encoding="utf-8", newline="")
    try:
        written = load_settings(path).service.retry_profile
    except ConfigError as error:
        raise CommandFailed(
            f"{path.name} no longer loads after setting retry_profile: {error}"
        ) from None
    if written != "demo":
        raise CommandFailed(f'set retry_profile = "demo" in [service] of {path.name} by hand')


def outage(args: argparse.Namespace, config: Config) -> int:
    raise CommandFailed("not built yet")


def verify(args: argparse.Namespace, config: Config) -> int:
    """`wsync verify --full`: through the running server when there is one, else on its own."""
    say(
        f"running a full verify: Razorpay's last {config.verify.days} days against the ledger, "
        "and the ledger against the sheet"
    )
    return verify_command(
        argparse.Namespace(config=args.config, full=True, repair=False, days=None, no_wait=False)
    )
