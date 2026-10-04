"""`wsync demo <scene>`: the scenes of the video, run on demand (showcase.md; Q105, Q120, Q181).

Every scene refuses, before it touches anything, unless the Razorpay key is a test-mode key and
the configured spreadsheet is on `[demo] spreadsheet_ids`, so a mistyped id can never empty a
real sheet (F36). Each prints what it is doing and what it waits for.
"""

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from webhook_sync.cli.admin import EXIT_REFUSED, CommandFailed
from webhook_sync.config import Config, ConfigError, load_config
from webhook_sync.receiver.demo import check_demo_switch

DEFAULT_CONFIG = Path("webhook-sync.toml")

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
    raise CommandFailed("not built yet")


def outage(args: argparse.Namespace, config: Config) -> int:
    raise CommandFailed("not built yet")


def verify(args: argparse.Namespace, config: Config) -> int:
    raise CommandFailed("not built yet")
