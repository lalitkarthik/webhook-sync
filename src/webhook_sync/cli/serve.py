"""`wsync serve`: start, failing closed, then serve the public app on `[service] public_listen`.

`start` is the startup sequence, in the order operations.md gives; later steps slot into it.
A refusal prints one line naming the key or variable and exits non-zero.
"""

import argparse
import asyncio
import logging
import random
import signal
import sys
from collections.abc import Callable, Generator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import override

import uvicorn
from fastapi import FastAPI

from webhook_sync.admin.app import create_admin_app
from webhook_sync.clock import utc_now
from webhook_sync.config import Config, ConfigError, load_config, locate_data_dir
from webhook_sync.db.lock import LockHeld, hold_lock
from webhook_sync.db.migrations import SchemaTooNew, apply_pending
from webhook_sync.db.writer import Writer
from webhook_sync.logs import configure_logging
from webhook_sync.queue import reclaim, register
from webhook_sync.receiver.app import configured_names, create_app, shut_down
from webhook_sync.receiver.demo import check_demo_switch

log = logging.getLogger("webhook_sync.serve")

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
        type=at_least_one,
        metavar="N",
        help="demo only: answer 500 to the first N attempts of each stored event",
    )
    parser.set_defaults(handler=serve)


def at_least_one(text: str) -> int:
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
            app = create_app(config, config.service.data_dir, args.demo_fail_first)
            admin = create_admin_app(app, config.service.data_dir)
            asyncio.run(
                _serve_until_stopped(
                    app,
                    admin,
                    split_listen(config.service.public_listen),
                    split_listen(config.service.admin_listen),
                )
            )
    except StartRefused as refusal:
        print(f"wsync serve: {refusal}", file=sys.stderr)
        return EXIT_REFUSED
    return 0


STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)
"""Ctrl-C and a platform's stop: both run the one shutdown (operations.md, Q37)."""


class _Listener(uvicorn.Server):
    """uvicorn without its own signal handlers: ours runs the shutdown, then stops it (G13)."""

    @override
    @contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


async def _serve_until_stopped(
    app: FastAPI, admin: FastAPI, public_at: tuple[str, int], admin_at: tuple[str, int]
) -> None:
    """Serve the public app and the admin app, each on its own listener, until SIGTERM or
    SIGINT; then shut down in operations.md's order: the app answers 503 to new deliveries,
    stops claiming, gives running jobs up to 20 s, releases the rest and writes the rejection
    counts while it still listens; then both listeners stop and the app's lifespan ends. One
    signal handler of ours stops both (G13). The caller releases the lock once this returns."""
    listener = _Listener(
        uvicorn.Config(app, host=public_at[0], port=public_at[1], log_config=None, access_log=False)
    )
    admin_listener = _Listener(
        uvicorn.Config(
            admin,
            host=admin_at[0],
            port=admin_at[1],
            log_config=None,
            access_log=False,
            lifespan="off",  # it shares the public app's writer and pauses
        )
    )
    stopping: list[asyncio.Task[None]] = []

    async def stop() -> None:
        try:
            if listener.started:  # otherwise the lifespan's end runs the shutdown
                await shut_down(app)
        except Exception:
            log.exception("shutdown step failed; stopping anyway")
        finally:
            listener.should_exit = True
            admin_listener.should_exit = True

    def on_signal() -> None:
        if stopping:
            return  # already shutting down
        log.info("shutting down: new deliveries answered 503")
        stopping.append(asyncio.create_task(stop()))

    with _handling_stop_signals(asyncio.get_running_loop(), on_signal):
        await asyncio.gather(listener.serve(), admin_listener.serve())


@contextmanager
def _handling_stop_signals(
    loop: asyncio.AbstractEventLoop, handler: Callable[[], None]
) -> Generator[None]:
    """Run `handler` on the loop at SIGINT or SIGTERM, restoring the old handlers afterwards.

    Windows' Proactor loop has no `add_signal_handler`, so there a plain `signal.signal`
    handler hands over to the loop (Ctrl-C arrives as SIGINT)."""
    restore: list[Callable[[], object]] = []
    for sig in STOP_SIGNALS:
        try:
            loop.add_signal_handler(sig, handler)
            restore.append(lambda sig=sig: loop.remove_signal_handler(sig))
        except NotImplementedError:
            previous = signal.signal(sig, lambda _sig, _frame: loop.call_soon_threadsafe(handler))
            restore.append(lambda sig=sig, previous=previous: signal.signal(sig, previous))
    try:
        yield
    finally:
        for undo in restore:
            undo()


@contextmanager
def start(
    config_path: Path,
    environ: Mapping[str, str] | None = None,
    demo_fail_first: int | None = None,
) -> Generator[Config]:
    """Run the startup sequence and yield the config, holding the lock until the block ends.

    Order (operations.md): take the lock → validate the config → refuse a newer schema → apply
    migrations → reclaim jobs left running (A13) → the caller starts listening, its lifespan
    starting the reaper. The lock file lives in the data directory, so only `[service]
    data_dir` is read before the lock. Raises StartRefused, holding nothing, if any step finds
    starting unsafe.
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
            asyncio.run(_prepare_database(config))
        except (ConfigError, LockHeld, SchemaTooNew) as error:
            raise StartRefused(str(error)) from None
        yield config


async def _prepare_database(config: Config) -> None:
    """Refuse a newer schema, apply pending migrations, give each configured source and sink
    its `sinks` row (so `wsync status` lists them with no server running), then reclaim jobs
    left running (A13)."""
    writer = await Writer.open(config.service.data_dir)
    try:
        await apply_pending(writer)
        await register(writer, configured_names(config))
        reclaimed = await reclaim(
            writer,
            retry_profile=config.service.retry_profile,
            now=utc_now(),
            rng=random.Random(),
        )
        if reclaimed:
            log.info("jobs left running by a previous run reclaimed: %d", len(reclaimed))
    finally:
        await writer.close()


def split_listen(listen: str) -> tuple[str, int]:
    """`host:port` (or `[ipv6]:port`) as a host and a port number."""
    host, _, port = listen.rpartition(":")
    return host.strip("[]"), int(port)
