"""`wsync serve`: start, failing closed, then serve the public app on `[service] public_listen`.

`start` is the startup sequence, in the order operations.md gives; later steps slot into it.
A refusal prints one line naming the key or variable and exits non-zero.
"""

import argparse
import asyncio
import sys
from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path

import uvicorn

from webhook_sync.config import Config, ConfigError, load_config, locate_data_dir
from webhook_sync.db.lock import LockHeld, hold_lock
from webhook_sync.db.migrations import SchemaTooNew, apply_pending
from webhook_sync.db.writer import Writer
from webhook_sync.logs import configure_logging
from webhook_sync.receiver.app import create_app
from webhook_sync.receiver.demo import check_demo_switch

DEFAULT_CONFIG = Path("webhook-sync.toml")
EXIT_REFUSED = 2


class StartRefused(Exception):
    """Starting is unsafe; the message is the one line `wsync serve` prints."""


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser("serve", help="receive webhooks on the public port")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="the config file (default: ./webhook-sync.toml)",
    )
    parser.add_argument(
        "--demo-fail-first",
        type=_at_least_one,
        metavar="N",
        help="demo only: answer 500 to the first N attempts of each stored event",
    )
    parser.set_defaults(handler=serve)


def _at_least_one(text: str) -> int:
    try:
        n = int(text)
    except ValueError:
        n = 0
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of at least 1, not {text!r}")
    return n


def serve(args: argparse.Namespace) -> int:
    configure_logging()
    try:
        with start(args.config, demo_fail_first=args.demo_fail_first) as config:
            host, port = split_listen(config.service.public_listen)
            app = create_app(config, config.service.data_dir, args.demo_fail_first)
            uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)
    except StartRefused as refusal:
        print(f"wsync serve: {refusal}", file=sys.stderr)
        return EXIT_REFUSED
    return 0


@contextmanager
def start(
    config_path: Path,
    environ: Mapping[str, str] | None = None,
    demo_fail_first: int | None = None,
) -> Generator[Config]:
    """Run the startup sequence and yield the config, holding the lock until the block ends.

    Order (operations.md): take the lock → validate the config → refuse a newer schema → apply
    migrations → (reclaim running jobs, #13) → the caller starts listening. The lock file lives
    in the data directory, so only `[service] data_dir` is read before the lock. Raises
    StartRefused, holding nothing, if any step finds starting unsafe.
    """
    with ExitStack() as held:
        try:
            data_dir = locate_data_dir(config_path)
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
                held.enter_context(hold_lock(data_dir))
            except OSError as error:
                raise ConfigError(
                    f"service.data_dir {data_dir} cannot be used: {error.strerror}"
                ) from None
            config = load_config(config_path, environ)
            if demo_fail_first is not None:  # part of validating the config (A6)
                check_demo_switch(config)
            asyncio.run(_prepare_database(config.service.data_dir))
        except (ConfigError, LockHeld, SchemaTooNew) as error:
            raise StartRefused(str(error)) from None
        yield config


async def _prepare_database(data_dir: Path) -> None:
    """Refuse a newer schema, then apply pending migrations."""
    writer = await Writer.open(data_dir)
    try:
        await apply_pending(writer)
    finally:
        await writer.close()


def split_listen(listen: str) -> tuple[str, int]:
    """`host:port` (or `[ipv6]:port`) as a host and a port number."""
    host, _, port = listen.rpartition(":")
    return host.strip("[]"), int(port)
