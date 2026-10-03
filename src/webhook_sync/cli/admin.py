"""`wsync resume <name>` and `wsync status` (Q133, Q134).

When a server holds the lock they ask it over the admin listener, so its claim-time cache is
cleared too; when none does they hold the lock themselves and act on the database (status
reads; resume updates `sinks`). Each says which way it went.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any, cast

import httpx

from webhook_sync.admin.app import BY_HEADER
from webhook_sync.admin.status import Status, format_status, read_status
from webhook_sync.clock import utc_now
from webhook_sync.config import ConfigError, locate_admin_listen, locate_data_dir
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


def _admin_call(method: str, admin_listen: str, path: str, ok: tuple[int, ...]) -> Any:
    url = f"http://{admin_listen}{path}"
    try:
        answer = httpx.request(
            method, url, headers={BY_HEADER: "cli"}, timeout=ADMIN_TIMEOUT_SECONDS
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
