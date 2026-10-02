"""`wsync serve`: load the config, start the public app on `[service] public_listen`.

The fail-closed checks and the lock file come with #9; this is the minimal start.
"""

import argparse
import sys
from pathlib import Path

import uvicorn

from webhook_sync.config import ConfigError, load_config
from webhook_sync.logs import configure_logging
from webhook_sync.receiver.app import create_app

DEFAULT_CONFIG = Path("webhook-sync.toml")


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser("serve", help="receive webhooks on the public port")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="the config file (default: ./webhook-sync.toml)",
    )
    parser.set_defaults(handler=serve)


def serve(args: argparse.Namespace) -> int:
    configure_logging()
    try:
        config = load_config(args.config)
    except ConfigError as error:
        print(f"wsync serve: {error}", file=sys.stderr)
        return 2
    host, port = split_listen(config.service.public_listen)
    app = create_app(config, config.service.data_dir)
    uvicorn.run(app, host=host, port=port, log_config=None, access_log=False)
    return 0


def split_listen(listen: str) -> tuple[str, int]:
    """`host:port` (or `[ipv6]:port`) as a host and a port number."""
    host, _, port = listen.rpartition(":")
    return host.strip("[]"), int(port)
