"""`wsync resume <name>` and `wsync status` (Q133, Q134); `wsync sweep`, `wsync backfill` and
`wsync verify` (Q152).

When a server holds the lock they ask it over the admin listener, so its claim-time cache is
cleared too; when none does they hold the lock themselves and act on the database (status
reads; resume updates `sinks`). Each says which way it went. A sweep, backfill or verify asked of
a server is a run: the server answers 202 with its id and the command polls it until it ends,
however long that takes (`verify --no-wait` prints the id and returns). With no server the
command does the same work itself under the lock, and repair jobs wait for the next `wsync serve`.
"""

import argparse
import asyncio
import logging
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine
from datetime import date
from pathlib import Path
from typing import Any, cast

import httpx

from webhook_sync.admin import runs
from webhook_sync.admin.app import BY_HEADER
from webhook_sync.admin.runs import Checks, Result, RunFailed, on_their_own
from webhook_sync.admin.status import Status, format_status, format_sweeps, read_status
from webhook_sync.cli.serve import at_least_one
from webhook_sync.clock import utc_now
from webhook_sync.config import ConfigError, load_config, locate_admin_listen, locate_data_dir
from webhook_sync.db.files import database_path
from webhook_sync.db.lock import lock_if_free
from webhook_sync.db.migrations import SchemaTooNew, apply_pending
from webhook_sync.db.writer import Writer
from webhook_sync.logs import configure_logging
from webhook_sync.queue import Resumed, resume

log = logging.getLogger("webhook_sync.admin")

DEFAULT_CONFIG = Path("webhook-sync.toml")
EXIT_FAILED = 1
EXIT_REFUSED = 2
ADMIN_TIMEOUT_SECONDS = 10.0
POLL_EVERY_SECONDS = 0.5
"""How often a command asks the server how its run is going."""

THROUGH_SERVER = "through the running server"
ON_DATABASE = "on the database: no server is running"


class CommandFailed(Exception):
    """The command could not do its work; the message is the one line it prints."""

    def __init__(self, message: str, exit_code: int = EXIT_FAILED) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def add_parsers(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    resume_parser = subparsers.add_parser("resume", help="clear a paused sink or source")
    resume_parser.add_argument("name", help="the sink or source, e.g. sheets or razorpay")
    _add_config(resume_parser)
    resume_parser.set_defaults(handler=resume_command)

    status_parser = subparsers.add_parser(
        "status", help="show sinks and sources, jobs waiting and dead, and the last flush"
    )
    _add_config(status_parser)
    status_parser.set_defaults(handler=status_command)

    sweep_parser = subparsers.add_parser("sweep", help="sweep Razorpay's lists once, now")
    _add_config(sweep_parser)
    sweep_parser.set_defaults(handler=sweep_command)

    backfill_parser = subparsers.add_parser(
        "backfill", help="bring in payments created on past dates; no checkpoint moves"
    )
    for flag, dest, says in (
        ("--from", "first", "the first local date (service.timezone)"),
        ("--to", "last", "the last local date, included"),
    ):
        backfill_parser.add_argument(
            flag, dest=dest, type=_local_date, required=True, metavar="YYYY-MM-DD", help=says
        )
    _add_config(backfill_parser)
    backfill_parser.set_defaults(handler=backfill_command)

    verify_parser = subparsers.add_parser(
        "verify", help="check the sheet against the ledger; with --full, Razorpay too"
    )
    verify_parser.add_argument("--full", action="store_true", help="check Razorpay too")
    verify_parser.add_argument(
        "--repair", action="store_true", help="repair what a full run finds (quick runs always do)"
    )
    verify_parser.add_argument(
        "--days",
        type=at_least_one,
        metavar="N",
        help="with --full, the days of payments checked (default: [verify] days)",
    )
    verify_parser.add_argument(
        "--no-wait",
        action="store_true",
        help="with a server running, print the run's id and return at once",
    )
    _add_config(verify_parser)
    verify_parser.set_defaults(handler=verify_command)


def _local_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"must be a date as YYYY-MM-DD, not {text!r}") from None


def _add_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="the config file (default: ./webhook-sync.toml)",
    )


def resume_command(args: argparse.Namespace) -> int:
    name: str = args.name
    try:
        result, way = _run(args.config, lambda d: _resume_on_database(d, name), _resume_via(name))
    except CommandFailed as failed:
        print(f"wsync resume: {failed}", file=sys.stderr)
        return failed.exit_code
    if result == "unknown":
        print(f"wsync resume: nothing is named {name!r} ({way}); see wsync status", file=sys.stderr)
        return EXIT_FAILED
    if result == "active":
        print(f"{name} is not paused ({way})")
    else:
        print(f"{name} resumed: claims start again ({way})")
    return 0


def status_command(args: argparse.Namespace) -> int:
    try:
        status, way = _run(args.config, _status_on_database, _status_via)
    except CommandFailed as failed:
        print(f"wsync status: {failed}", file=sys.stderr)
        return failed.exit_code
    print(f"server: {way}")
    print(format_status(status))
    return 0


def sweep_command(args: argparse.Namespace) -> int:
    try:
        result, way = run_by_hand(args.config, "/admin/sweep", None, runs.sweep)
    except CommandFailed as failed:
        print(f"wsync sweep: {failed}", file=sys.stderr)
        return failed.exit_code
    if result["finished"]:
        print(f"sweep done ({way})")
    print("\n".join(format_sweeps(result["sweeps"])))
    if not result["finished"]:
        print(
            "wsync sweep: stopped before catching up: Razorpay is paused or a list failed;"
            f" see the log ({way})",
            file=sys.stderr,
        )
        return EXIT_FAILED
    return 0


def backfill_command(args: argparse.Namespace) -> int:
    first: date = args.first
    last: date = args.last
    if first > last:
        print(f"wsync backfill: --from {first} is after --to {last}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        result, way = run_by_hand(
            args.config,
            "/admin/backfill",
            {"from": first.isoformat(), "to": last.isoformat()},
            lambda checks: runs.backfill(checks, first, last),
        )
    except CommandFailed as failed:
        print(f"wsync backfill: {failed}", file=sys.stderr)
        return failed.exit_code
    print(f"backfill {first} to {last}: {result['stored']} events stored ({way})")
    if way == ON_DATABASE and result["stored"]:
        print("their ledger jobs run at the next wsync serve")
    return 0


def verify_command(args: argparse.Namespace) -> int:
    full: bool = args.full
    repair: bool = args.repair
    days: int | None = args.days
    if days is not None and not full:
        print("wsync verify: --days applies to a full run only; add --full", file=sys.stderr)
        return EXIT_REFUSED
    try:
        result, way = run_by_hand(
            args.config,
            "/admin/verify",
            {"full": full, "repair": repair, "days": days},
            lambda checks: runs.verify(checks, full=full, repair=repair, days=days),
            wait=not args.no_wait,
        )
    except CommandFailed as failed:
        print(f"wsync verify: {failed}", file=sys.stderr)
        return failed.exit_code
    if "run_id" in result:
        print(f"run {result['run_id']} started ({way})")
        return 0
    print(result["line"])
    if full and result["kind"] == "quick":
        print("Razorpay is paused: only the sheet was checked")
    print(f"{result['findings_count']} findings, {result['repaired_count']} repaired ({way})")
    for finding in result["findings"]:
        line = f"{finding['kind']} {finding['payment_id'] or '-'}"
        if finding["detail"]:
            line += f": {finding['detail']}"
        if finding["repaired"]:
            line += " (repair queued)"
        print(line)
    if way == ON_DATABASE and result["repaired_count"]:
        print("repair jobs run at the next wsync serve")
    return 0


def run_by_hand(
    config_path: Path,
    path: str,
    body: object,
    work: Callable[[Checks], Coroutine[Any, Any, Result]],
    *,
    wait: bool = True,
) -> tuple[Result, str]:
    """A run asked of the server at `path` and polled until it ends (with `wait` False, only
    started: its id), or `work` done here under the lock when no server holds it."""

    async def on_database(data_dir: Path) -> Result:
        try:
            config = load_config(config_path)
        except ConfigError as error:
            raise CommandFailed(str(error), EXIT_REFUSED) from None
        configure_logging()

        async def act(writer: Writer) -> Result:
            async with on_their_own(config, writer) as checks:
                try:
                    return await work(checks)
                except RunFailed as failed:
                    raise CommandFailed(str(failed)) from None

        return await _on_migrated_database(data_dir, act)

    def via_admin(admin_listen: str) -> Result:
        started = _admin_call("POST", admin_listen, path, ok=(202,), body=body)
        run_id: str = started["run_id"]
        if not wait:
            return {"run_id": run_id}
        while True:
            time.sleep(POLL_EVERY_SECONDS)
            report = _admin_call("GET", admin_listen, f"/admin/runs/{run_id}", ok=(200, 404))
            if "state" not in report:
                raise CommandFailed(f"the server no longer knows run {run_id}: it restarted")
            if report["state"] == "finished":
                return report["result"]
            if report["state"] == "failed":
                raise CommandFailed(report["error"])

    return _run(config_path, on_database, via_admin)


def _run[T](
    config_path: Path,
    on_database: Callable[[Path], Coroutine[Any, Any, T]],
    via_admin: Callable[[str], T],
) -> tuple[T, str]:
    """Through the admin listener when a server holds the lock, else on the database."""
    try:
        data_dir = locate_data_dir(config_path)
        admin_listen = locate_admin_listen(config_path)
    except ConfigError as error:
        raise CommandFailed(str(error), EXIT_REFUSED) from None
    if not database_path(data_dir).is_file():
        raise CommandFailed(f"no database in {data_dir}: wsync serve has not run there yet")
    with lock_if_free(data_dir) as free:
        if free:
            return asyncio.run(on_database(data_dir)), ON_DATABASE
    return via_admin(admin_listen), THROUGH_SERVER


async def _on_migrated_database[T](data_dir: Path, act: Callable[[Writer], Awaitable[T]]) -> T:
    writer = await Writer.open(data_dir)
    try:
        try:
            await apply_pending(writer)
        except SchemaTooNew as error:
            raise CommandFailed(str(error), EXIT_REFUSED) from None
        return await act(writer)
    finally:
        await writer.close()


async def _resume_on_database(data_dir: Path, name: str) -> Resumed:
    async def act(writer: Writer) -> Resumed:
        result = await resume(writer, name, pauses=None, now=utc_now())
        if result == "resumed":
            configure_logging()
            log.info("pause cleared", extra={"target": name, "by": "cli"})
        return result

    return await _on_migrated_database(data_dir, act)


async def _status_on_database(data_dir: Path) -> Status:
    async def act(writer: Writer) -> Status:
        return await read_status(data_dir)

    return await _on_migrated_database(data_dir, act)


def _resume_via(name: str) -> Callable[[str], Resumed]:
    def call(admin_listen: str) -> Resumed:
        answer = _admin_call("POST", admin_listen, f"/admin/resume/{name}", ok=(200, 404))
        return cast(Resumed, answer["result"])

    return call


def _status_via(admin_listen: str) -> Status:
    return cast(Status, _admin_call("GET", admin_listen, "/admin/status", ok=(200,)))


def _admin_call(
    method: str, admin_listen: str, path: str, ok: tuple[int, ...], body: object = None
) -> Any:
    url = f"http://{admin_listen}{path}"
    try:
        answer = httpx.request(
            method, url, headers={BY_HEADER: "cli"}, json=body, timeout=ADMIN_TIMEOUT_SECONDS
        )
    except httpx.HTTPError:
        raise CommandFailed(
            f"a server holds the lock but its admin listener at {admin_listen} does not answer"
        ) from None
    if answer.status_code == 503:
        raise CommandFailed("the server is starting or stopping; try again in a moment")
    if answer.status_code not in ok:
        raise CommandFailed(f"the admin listener answered HTTP {answer.status_code}")
    return answer.json()
