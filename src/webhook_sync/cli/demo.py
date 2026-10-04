"""`wsync demo <scene>`: the scenes of the video, run on demand (showcase.md; Q105, Q120, Q181).

Every scene refuses, before it touches anything, unless the Razorpay key is a test-mode key and
the configured spreadsheet is on `[demo] spreadsheet_ids`, so a mistyped id can never empty a
real sheet (F36). Each prints what it is doing and what it waits for.

- `reset` empties the database and our three tabs of the demo sheet, and sets the demo retry
  profile in the config file. It needs the lock, so it refuses while a server runs.
- `outage` pays test orders with the receiver stopped (the payer, else a person at the page it
  prints), then starts its own receiver, sweeps until each payment is stored, waits for the
  ledger and sheet rows, and stops the receiver. It refuses while a server runs.
- `verify` runs `wsync verify --full`, through the running server when there is one.
"""

import argparse
import asyncio
import copy
import functools
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import sqlalchemy as sa

from webhook_sync.admin import runs
from webhook_sync.cli.admin import EXIT_REFUSED, CommandFailed, run_by_hand, verify_command
from webhook_sync.cli.doctor import sheet_failure, stored_tabs
from webhook_sync.cli.serve import at_least_one, prepare_database, split_listen
from webhook_sync.config import Config, ConfigError, load_config
from webhook_sync.db.files import database_path
from webhook_sync.db.lock import lock_if_free
from webhook_sync.db.reader import Reader
from webhook_sync.db.schema import events, jobs, payments
from webhook_sync.demo.payer import CheckoutPages, PayerFailed, load_payer
from webhook_sync.receiver.demo import check_demo_switch
from webhook_sync.sinks.sheets import plan_tabs_reset
from webhook_sync.sinks.sheets_client import ServiceAccountTokens, SheetsClient, SheetsFailure

DEFAULT_CONFIG = Path("webhook-sync.toml")
DATABASE_SUFFIXES = ("", "-wal", "-shm", "-journal")
"""The database file and the files SQLite keeps beside it."""
SERVE_LOG = "demo-serve.log"
"""The receiver's log while a scene runs it, in the data directory."""

ORDERS = 3
AMOUNT = 50_000
"""INR 500 in paise: netbanking at INR 100 or more shows a fee and GST (Q105)."""
CURRENCY = "INR"
RAZORPAY_TIMEOUT = 10.0
CAPTURED_WITHIN = 15 * 60.0
"""Real seconds an order has to be paid and captured, a person paying by hand included."""
POLL_EVERY = 5.0
"""Real seconds between two looks at an order's payments."""
POLL_AT_MOST = 30.0
HEALTHY_WITHIN = 30.0
STOP_WITHIN = 30.0
SWEEP_AGAIN_AFTER = 5.0
STORED_WITHIN = 5 * 60.0
ROWS_WITHIN = 2 * 60.0

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
        if scene is outage:
            scene_parser.add_argument(
                "--orders",
                type=at_least_one,
                default=ORDERS,
                metavar="N",
                help=f"the test orders paid while the receiver is stopped (default: {ORDERS})",
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
    profile until it restarts, and its database is not emptied under it (ADR 0007).

    What can be checked is checked before anything is emptied: the config file's new text is
    proved and the file opened for writing, and the sheet is read for a tab with one of our
    titles that is not ours. The sheet is written first, the database emptied next and the
    config file written last, so a refused sheet write leaves all three as they were."""
    with_profile = with_demo_retry_profile(args.config)
    data_dir = config.service.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    with lock_if_free(data_dir) as free:
        if not free:
            raise CommandFailed(
                "a server is running: stop wsync serve first; reset empties its database",
                EXIT_REFUSED,
            )
        asyncio.run(_empty(config))
        try:
            args.config.write_text(with_profile, encoding="utf-8", newline="")
        except OSError as error:
            raise CommandFailed(
                _by_hand(args.config, f"the file cannot be written ({error.strerror})")
            ) from None
        say(
            f'retry_profile = "demo" set in [service] of {args.config.name}: '
            "failed jobs retry after 1, 2, 4, 8 and 16 s from the next wsync serve"
        )
    return 0


async def _empty(config: Config) -> None:
    """Plan the sheet's reset from the tab ids the database still holds, write it, then empty
    the database. A sheet write Google refuses (a Viewer's 403, a quota, a timeout) leaves the
    database with those ids; if the database step fails after it, the next reset finds the new
    tabs by title and header."""
    account = f"the service account {config.google_secrets.service_account.get('client_email')}"
    tokens = ServiceAccountTokens(config.google_secrets.service_account)
    try:
        stored = stored_tabs(config)
    except sqlite3.Error:
        stored = None  # an unreadable database holds no ids: ours are then found by header
    async with httpx.AsyncClient() as http:
        client = SheetsClient(http, config.sheets.spreadsheet_id, tokens)
        plan = await plan_tabs_reset(client, config, stored)
        if isinstance(plan, SheetsFailure):
            raise CommandFailed(sheet_failure(plan, account))
        if plan.not_ours:
            named = " and ".join(plan.not_ours)
            raise CommandFailed(
                f"a tab named {named} is not ours: its header differs from the configured "
                "columns, so reset will not delete it; rename or delete it, then run reset again",
                EXIT_REFUSED,
            )
        say("emptying the demo sheet's Payments, Daily summary and Sync status tabs")
        written = await client.batch_update(plan.requests)
        if isinstance(written, SheetsFailure):
            raise CommandFailed(sheet_failure(written, account))
        say(
            "Payments, Daily summary and Sync status emptied, each with its header; "
            "the sheet's other tabs are untouched"
        )
    database = database_path(config.service.data_dir)
    say(f"emptying the database {database}")
    for suffix in DATABASE_SUFFIXES:
        database.with_name(database.name + suffix).unlink(missing_ok=True)
    await prepare_database(config)
    say("database emptied: migrated, with no events, jobs or payments")


_TABLE = re.compile(r"\s*\[")
_SERVICE_TABLE = re.compile(r"""\[\s*(service|"service"|'service')\s*\]""")
_ROOT_SERVICE_KEY = re.compile(r"\s*service\s*\.")
_PROFILE_VALUE = re.compile(
    r"""(\s*(?:service\s*\.\s*)?retry_profile\s*=\s*)("[^"\\]*"|'[^']*')""", re.DOTALL
)


def with_demo_retry_profile(path: Path) -> str:
    """The config file's text with `retry_profile = "demo"` in its `[service]` table: the
    value replaced where the key is set, the rest of its line kept, or one line added. Nothing
    is written here, and the new text is proved first: it must parse to the same document with
    only that key changed. A file that can't be edited so, or can't be written, is refused,
    untouched."""
    text = path.read_text(encoding="utf-8", newline="")
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if lines and lines[0].endswith("\r\n") else "\n"
    at_root, in_service = True, False  # where the line being read is
    service_at: int | None = None
    dotted = False
    for at, line in enumerate(lines):
        if _TABLE.match(line):
            at_root = False
            in_service = _SERVICE_TABLE.fullmatch(line.split("#")[0].strip()) is not None
            if in_service and service_at is None:
                service_at = at
            continue
        dotted = dotted or (at_root and _ROOT_SERVICE_KEY.match(line) is not None)
        set_here = _PROFILE_VALUE.match(line)
        # At the root the key is `service.retry_profile`; in the table, `retry_profile`.
        if set_here and (in_service or at_root) and at_root == ("service" in set_here[1]):
            lines[at] = f'{set_here[1]}"demo"{line[set_here.end() :]}'
            break
    else:
        if service_at is not None:
            if not lines[service_at].endswith(("\n", "\r")):
                lines[service_at] += newline
            lines.insert(service_at + 1, f'retry_profile = "demo"{newline}')
        elif dotted:
            lines.insert(0, f'service.retry_profile = "demo"{newline}')
        else:
            lines[:0] = [f"[service]{newline}", f'retry_profile = "demo"{newline}', newline]
    changed = "".join(lines)
    try:
        wanted = copy.deepcopy(tomllib.loads(text))
        wanted.setdefault("service", {})["retry_profile"] = "demo"
        proved = tomllib.loads(changed) == wanted
    except tomllib.TOMLDecodeError, TypeError:
        proved = False
    if not proved:
        raise CommandFailed(
            _by_hand(path, "this file is laid out in a way reset will not edit"), EXIT_REFUSED
        )
    try:
        with path.open("r+b"):  # opened for writing, and nothing written
            pass
    except OSError as error:
        raise CommandFailed(
            _by_hand(path, f"the file cannot be written ({error.strerror})"), EXIT_REFUSED
        ) from None
    return changed


def _by_hand(path: Path, why: str) -> str:
    return (
        f'set retry_profile = "demo" in [service] of {path.name} by hand, then run reset '
        f"again: {why}"
    )


def outage(args: argparse.Namespace, config: Config) -> int:
    """With the receiver stopped, pay test orders; then start it, sweep until every payment
    is stored, wait for their ledger and sheet rows, and stop it. It starts and stops its own
    receiver, so it refuses while a server runs."""
    data_dir = config.service.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    with lock_if_free(data_dir) as free:
        if not free:
            raise CommandFailed(
                "a server is running: stop wsync serve first; outage starts and stops its own",
                EXIT_REFUSED,
            )
    say("the receiver is stopped: Razorpay's deliveries of these payments reach nobody")
    paid = _pay_orders(config, args.orders)
    with _receiver(args.config, config):
        _sweep_until_stored(args.config, data_dir, paid)
        say("waiting for their ledger rows and sheet rows: the flusher writes every 3 s")
        rows = _rows_written(data_dir, paid)
        for payment_id in paid:
            say(f"{payment_id}: one ledger row and one sheet row, Via = {rows[payment_id]}")
    return 0


def _pay_orders(config: Config, count: int) -> list[str]:
    """Make `count` orders and have each paid, by the payer or else by a person at its page;
    the captured payments' ids, in order."""
    payer = load_payer(config, os.environ)
    secrets = config.razorpay_secrets
    paid: list[str] = []
    with (
        CheckoutPages(secrets.key_id) as pages,
        httpx.Client(
            base_url=config.razorpay.api_url,
            auth=httpx.BasicAuth(secrets.key_id, secrets.key_secret),
            timeout=RAZORPAY_TIMEOUT,
        ) as api,
    ):
        for n in range(1, count + 1):
            order = _razorpay(
                api,
                "POST",
                "/v1/orders",
                json={"amount": AMOUNT, "currency": CURRENCY, "receipt": f"wsync-demo-{n}"},
            )
            if order is None:
                raise CommandFailed("Razorpay answered 429 (too many requests) to a new order")
            checkout = pages.add(order["id"], AMOUNT, CURRENCY)
            say(
                f"order {n} of {count}: {checkout.order_id} made for {CURRENCY} {AMOUNT / 100:.2f};"
                " paying it headless: Netbanking, a bank, then Success on the mock bank page"
            )
            try:
                payer.pay(checkout)
            except PayerFailed as failed:
                say(
                    f"the headless payment failed ({failed}); pay it by hand at "
                    f"{checkout.page_url}: Netbanking, any bank, then Success"
                )
            say(f"waiting for Razorpay to capture the payment of {checkout.order_id}")
            payment_id = _captured(api, checkout.order_id)
            say(f"{payment_id} captured")
            paid.append(payment_id)
    return paid


def _razorpay(api: httpx.Client, method: str, path: str, **sent: Any) -> Any:
    """Razorpay's answer to one call, or None when it answers 429: asked too often."""
    try:
        answer = api.request(method, path, **sent)
    except httpx.HTTPError as error:
        raise CommandFailed(f"Razorpay could not be reached: {type(error).__name__}") from None
    if answer.status_code == 429:
        return None
    if answer.status_code != 200:
        try:
            said = answer.json()["error"]["description"]
        except ValueError, KeyError, TypeError:
            said = "no description"
        raise CommandFailed(
            f"Razorpay answered {method} {path} with HTTP {answer.status_code}: {said}"
        )
    return answer.json()


def _captured(api: httpx.Client, order_id: str) -> str:
    """The id of the order's captured payment, once Razorpay has one. A 429 doubles the time
    between two looks, up to `POLL_AT_MOST`: a person paying by hand can take minutes."""
    deadline = time.monotonic() + CAPTURED_WITHIN
    every = POLL_EVERY
    while True:
        listed = _razorpay(api, "GET", f"/v1/orders/{order_id}/payments")
        if listed is None:
            every = min(2 * every, POLL_AT_MOST)
            say(f"Razorpay answered 429 (too many requests): asking every {every:.0f} s")
        else:
            for payment in listed["items"]:
                if payment["status"] == "captured":
                    return payment["id"]
        if time.monotonic() > deadline:
            raise CommandFailed(
                f"no payment of {order_id} captured in {CAPTURED_WITHIN / 60:.0f} minutes"
            )
        time.sleep(every)


@contextmanager
def _receiver(config_path: Path, config: Config) -> Generator[None]:
    """`wsync serve` on the config, started and healthy while the block runs, then stopped."""
    log_path = config.service.data_dir / SERVE_LOG
    host, port = split_listen(config.service.public_listen)
    base_url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    say(f"starting the receiver: wsync serve, its log in {log_path}")
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "webhook_sync", "serve", "--config", str(config_path)],
            stdout=log,
            stderr=log,
        )
    try:
        _healthy(base_url, process, log_path)
        say(f"the receiver is up at {base_url}")
        yield
    finally:
        say("stopping the receiver")
        stop_receiver(process, config.service.data_dir)
        say("the receiver is stopped")


def _healthy(base_url: str, process: subprocess.Popen[bytes], log_path: Path) -> None:
    deadline = time.monotonic() + HEALTHY_WITHIN
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise CommandFailed(f"the receiver stopped as it started; see {log_path}")
        try:
            if httpx.get(f"{base_url}/health", timeout=1, trust_env=False).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    raise CommandFailed(f"the receiver did not answer /health in {HEALTHY_WITHIN:.0f} s")


def _ask_to_stop(process: subprocess.Popen[bytes]) -> None:
    """SIGTERM, the server's own shutdown; on Windows, where a console process can't be sent
    one, the process tree is ended: everything the scene waited for is already written, and a
    stop at any point loses nothing (the crash tests)."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False
        )
    else:
        process.send_signal(signal.SIGTERM)


def stop_receiver(
    process: subprocess.Popen[bytes],
    data_dir: Path,
    *,
    within: float = STOP_WITHIN,
    ask: Callable[[subprocess.Popen[bytes]], None] = _ask_to_stop,
) -> None:
    """Ask the receiver to stop and give it `within` seconds, the grace of its own shutdown
    (operations.md); then kill it. Returns once the lock is free, and fails the scene when it
    still is not `within` seconds later: something else holds the database."""
    ask(process)
    try:
        process.wait(timeout=within)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        say(f"the receiver did not stop in {within:g} s and was killed")
    deadline = time.monotonic() + within
    while True:
        with lock_if_free(data_dir) as free:
            if free:
                return
        if time.monotonic() > deadline:
            raise CommandFailed(
                f"the receiver was stopped but the lock is still held {within:g} s later: "
                "a wsync serve still runs on this database; stop it by hand"
            )
        time.sleep(0.2)


def _sweep_until_stored(config_path: Path, data_dir: Path, paid: list[str]) -> None:
    """Sweeps through the receiver until each payment is stored. Razorpay's list gives a sweep
    only payments a minute old (sweeper.md), so a payment just made waits for a later one; a
    sweep that stops short, a list call failing, is tried again too."""
    deadline = time.monotonic() + STORED_WITHIN
    say("sweeping Razorpay's payments since the checkpoint")
    while True:
        result, _ = run_by_hand(config_path, "/admin/sweep", None, runs.sweep)
        missing = len(paid) - len(asyncio.run(_stored(data_dir, paid)))
        if not missing:
            say(f"swept: all {len(paid)} payments stored, each as a sweep event")
            return
        if time.monotonic() > deadline:
            raise CommandFailed(
                f"{missing} of {len(paid)} payments still not stored after "
                f"{STORED_WITHIN / 60:.0f} minutes of sweeps; see wsync status"
            )
        if result["finished"]:
            say(
                f"{missing} of {len(paid)} payments not listed yet: a sweep reads only payments "
                f"a minute old at Razorpay; sweeping again in {SWEEP_AGAIN_AFTER:.0f} s"
            )
        else:
            say(
                "the sweep stopped before catching up: a list call to Razorpay failed; "
                f"sweeping again in {SWEEP_AGAIN_AFTER:.0f} s"
            )
        time.sleep(SWEEP_AGAIN_AFTER)


async def _stored(data_dir: Path, paid: list[str]) -> set[str]:
    async with Reader.open(data_dir) as reader:
        found = await reader.fetch_all(
            sa.select(events.c.payment_key).where(events.c.payment_key.in_(paid)).distinct()
        )
    return {row["payment_key"] for row in found}


def _rows_written(data_dir: Path, paid: list[str]) -> dict[str, str]:
    """Each payment's Via, once it has its ledger row and no job of it is waiting or running:
    its mirror job is done, so its sheet row is written."""
    deadline = time.monotonic() + ROWS_WITHIN
    while True:
        rows, open_jobs, dead = asyncio.run(_ledger(data_dir, paid))
        if dead:
            raise CommandFailed(f"a job of {dead[0]} went dead; see wsync status")
        if len(rows) == len(paid) and not open_jobs:
            return rows
        if time.monotonic() > deadline:
            raise CommandFailed("the rows were not all written in time; see wsync status")
        time.sleep(0.5)


async def _ledger(data_dir: Path, paid: list[str]) -> tuple[dict[str, str], list[str], list[str]]:
    async with Reader.open(data_dir) as reader:
        rows = await reader.fetch_all(
            sa.select(payments.c.payment_id, payments.c.via).where(payments.c.payment_id.in_(paid))
        )
        found = await reader.fetch_all(
            sa.select(jobs.c.payment_key, jobs.c.status).where(jobs.c.payment_key.in_(paid))
        )
    open_jobs = [job["payment_key"] for job in found if job["status"] in ("waiting", "running")]
    dead = [job["payment_key"] for job in found if job["status"] == "dead"]
    return {row["payment_id"]: row["via"] for row in rows}, open_jobs, dead


def verify(args: argparse.Namespace, config: Config) -> int:
    """`wsync verify --full`: through the running server when there is one, else on its own."""
    say(
        f"running a full verify: Razorpay's last {config.verify.days} days against the ledger, "
        "and the ledger against the sheet"
    )
    return verify_command(
        argparse.Namespace(config=args.config, full=True, repair=False, days=None, no_wait=False)
    )
