"""`wsync doctor`: check a setup end to end and say in plain words what is wrong (Q180, Q182).

One line per check, `ok`, `warning` or `fail` with the reason, and exit code 0 unless a check
fails. The checks, in the order printed:

- `config`: the TOML parses and validates;
- `secrets`: every secret is present, and the Google one is a service-account key;
- `razorpay key`: its prefix is the configured mode's, and Razorpay accepts it;
- `razorpay webhook`: a webhook is listed (`GET /v1/webhooks`), active, with a secret and every
  handled event; only ever a warning, and a 4xx reads "can't check": Razorpay documents that
  list for partners only (Q111);
- `clock`: ours is within 30 s of the `Date` header Razorpay answers with;
- `sheet`: the spreadsheet opens with the service account, and its tabs and headers are what a
  flush expects;
- `database`: writable, migrated, not newer than this code, WAL with `synchronous=FULL`, and
  on a local disk: not a network drive or a synced folder, where WAL is unsafe;
- `public url`, with `--url`: an empty POST gets the receiver's own 400. A 200 with an HTML
  page is a tunnel's warning page, which would swallow real deliveries (Q112).

Each check stands alone: one that fails doesn't stop the next, and one that needs what an
earlier one found missing says so in its own line. `doctor` runs with or without a server,
takes no lock and writes nothing: it reads the config, Razorpay, the sheet and the database.
Nothing here prints a secret's value or the spreadsheet id.
"""

import argparse
import asyncio
import os
import socket
import sqlite3
import sys
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path, PurePath
from typing import Any, Literal, cast

import httpx

from webhook_sync.clock import utc_now
from webhook_sync.config import (
    SECRET_NAMES,
    ConfigError,
    Settings,
    check_key_mode,
    load_settings,
    locate_data_dir,
    read_secrets,
    service_account_key,
    webhook_secrets,
)
from webhook_sync.db.files import database_path
from webhook_sync.db.migrations import known_migrations
from webhook_sync.sinks.sheets import Layout, TabIds, read_layout
from webhook_sync.sinks.sheets_client import (
    KEY_REFUSED,
    SPREADSHEET_MISSING,
    ServiceAccountTokens,
    SheetsClient,
    SheetsFailure,
)
from webhook_sync.sources.razorpay import HANDLED_TYPES, api_client

DEFAULT_CONFIG = Path("webhook-sync.toml")
EXIT_FAILED = 1

CLOCK_LIMIT = timedelta(seconds=30)
"""How far our clock may be from Razorpay's before the check fails (operations.md)."""

URL_TIMEOUT = timedelta(seconds=10)
"""How long the public URL may take to answer the empty POST."""

WEBHOOKS_PAGE = 100

type State = Literal["ok", "warning", "fail"]
type Report = Callable[[Line], None]


@dataclass(frozen=True)
class Line:
    """One check's line: its state, its name and the reason in plain words."""

    state: State
    check: str
    says: str

    def __str__(self) -> str:
        return f"{self.state:<8}{self.check}: {self.says}"


@dataclass(frozen=True)
class _Probe:
    """Razorpay's answer to one small documented call, and our time when it came."""

    response: httpx.Response
    at: datetime


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:  # pyright: ignore[reportPrivateUsage]
    parser = subparsers.add_parser(
        "doctor", help="check a setup end to end and say what is wrong; changes nothing"
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="the config file (default: ./webhook-sync.toml)",
    )
    parser.add_argument(
        "--url",
        metavar="URL",
        help="also POST an empty body to the public webhook URL: it must get the receiver's 400",
    )
    parser.set_defaults(handler=doctor_command)


def doctor_command(args: argparse.Namespace) -> int:
    states: list[State] = []

    def report(line: Line) -> None:
        states.append(line.state)
        print(line, flush=True)

    asyncio.run(examine(args.config, args.url, report))
    return EXIT_FAILED if "fail" in states else 0


async def examine(config_path: Path, url: str | None, report: Report) -> None:
    """Run every check, handing each one's line to `report` as it ends."""
    settings: Settings | None = None
    try:
        settings = load_settings(config_path)
        report(Line("ok", "config", f"{config_path.name} parses and validates"))
    except ConfigError as error:
        report(Line("fail", "config", str(error)))
    secrets = read_secrets(config_path)
    report(_secrets(secrets))

    key_id, key_secret = secrets["RAZORPAY_KEY_ID"], secrets["RAZORPAY_KEY_SECRET"]
    if settings is None or not key_id or not key_secret:
        report(_key(settings, key_id, None))
        report(Line("warning", "razorpay webhook", f"can't check: {_needs_key(settings)}"))
        report(Line("fail", "clock", f"not checked: {_needs_key(settings)}"))
    else:
        async with api_client(settings.razorpay.api_url, key_id, key_secret) as api:
            probe = await _probe(api)
            report(_key(settings, key_id, probe))
            report(await _webhook(api, url))
            report(_clock(probe))
    report(await _sheet(settings, secrets["GOOGLE_SERVICE_ACCOUNT_B64"]))
    report(_database(config_path, settings))
    if url is not None:
        report(await _public_url(url))


def _needs_key(settings: Settings | None) -> str:
    if settings is None:
        return "the config file must load first"
    return "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set first"


# -- secrets, key and mode -----------------------------------------------------------------------


def _secrets(secrets: Mapping[str, str]) -> Line:
    missing = [name for name in SECRET_NAMES if not secrets[name]]
    problems = [f"{name} is missing or empty" for name in missing]
    for name, check in (
        ("RAZORPAY_WEBHOOK_SECRET", webhook_secrets),
        ("GOOGLE_SERVICE_ACCOUNT_B64", service_account_key),
    ):
        if name not in missing:
            try:
                check(secrets[name])
            except ConfigError as error:
                problems.append(str(error))
    if problems:
        return Line("fail", "secrets", "; ".join(problems))
    return Line("ok", "secrets", "every secret is present")


def _key(settings: Settings | None, key_id: str, probe: _Probe | str | None) -> Line:
    """The key's prefix against the configured mode, then whether Razorpay accepts the key."""
    check = "razorpay key"
    if settings is None:
        return Line("fail", check, "not checked: the config file must load first")
    if not key_id:
        return Line("fail", check, "not checked: RAZORPAY_KEY_ID is missing")
    mode = settings.razorpay.mode
    try:
        check_key_mode(mode, key_id)
    except ConfigError as error:
        return Line("fail", check, str(error))
    matches = f"a {mode}-mode key, as sources.razorpay.mode says"
    if probe is None:
        return Line(
            "warning", check, f"{matches}; not tried at Razorpay: RAZORPAY_KEY_SECRET is missing"
        )
    if isinstance(probe, str):
        return Line("fail", check, f"{matches}, but {probe}")
    status = probe.response.status_code
    if status == 401:
        return Line(
            "fail",
            check,
            "Razorpay refused the key (HTTP 401): RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be"
            " one key pair, as the Dashboard generated them",
        )
    if not probe.response.is_success:
        return Line(
            "warning", check, f"{matches}; not tried: Razorpay answered HTTP {status} just now"
        )
    return Line("ok", check, f"{matches}, and Razorpay accepts it")


async def _probe(api: httpx.AsyncClient) -> _Probe | str:
    """One payment of the last minute asked for: the smallest documented call. Its status says
    whether the key is accepted, its `Date` header is Razorpay's clock."""
    now = int(utc_now().timestamp())
    params = {"from": str(now - 60), "to": str(now), "count": "1"}
    try:
        response = await api.get("/v1/payments", params=params)
    except httpx.TimeoutException:
        return "Razorpay did not answer within 10 s"
    except httpx.TransportError as error:
        return f"Razorpay could not be reached ({type(error).__name__})"
    return _Probe(response, utc_now())


def _clock(probe: _Probe | str) -> Line:
    if isinstance(probe, str):
        return Line("fail", "clock", f"not checked: {probe}")
    try:
        theirs = parsedate_to_datetime(probe.response.headers.get("date", ""))
    except TypeError, ValueError:
        return Line("fail", "clock", "not checked: Razorpay's answer had no readable Date header")
    if theirs.tzinfo is None:
        theirs = theirs.replace(tzinfo=UTC)  # an HTTP date is always GMT
    off = (probe.at - theirs).total_seconds()
    limit = int(CLOCK_LIMIT.total_seconds())
    if abs(off) > limit:
        way = "ahead of" if off > 0 else "behind"
        return Line(
            "fail",
            "clock",
            f"our clock is {abs(off):.0f} s {way} Razorpay's, more than {limit} s: set the "
            "system clock, or turn its time sync on",
        )
    return Line("ok", "clock", f"within {limit} s of Razorpay's ({abs(off):.0f} s off)")


# -- Razorpay's webhook ----------------------------------------------------------------------------


async def _webhook(api: httpx.AsyncClient, url: str | None) -> Line:
    """Razorpay's webhook for this deployment: with `--url` the one posting there, otherwise
    any. Never a failure: the list is documented only for partners (Q111)."""
    check = "razorpay webhook"
    try:
        response = await api.get("/v1/webhooks", params={"count": str(WEBHOOKS_PAGE)})
    except httpx.TransportError as error:
        return Line("warning", check, f"can't check: no answer ({type(error).__name__})")
    try:
        body: object = response.json()
    except ValueError:
        body = None
    items: object = cast(dict[str, Any], body).get("items") if isinstance(body, dict) else None
    if not response.is_success or not isinstance(items, list):
        return Line(
            "warning",
            check,
            f"can't check: Razorpay answered HTTP {response.status_code} to the webhook list, "
            "which it documents only for partners; look at Dashboard > Settings > Webhooks",
        )
    webhooks = [cast(dict[str, Any], w) for w in cast(list[object], items) if isinstance(w, dict)]
    if url is not None:
        webhooks = [w for w in webhooks if _same_url(w.get("url"), url)]
    if not webhooks:
        where = f"posts to {url}" if url is not None else "is set up for this key's mode"
        return Line(
            "warning",
            check,
            f"no webhook {where}: payments arrive only by the sweeper until one is added "
            "(Dashboard > Settings > Webhooks)",
        )
    found = [_webhook_problems(webhook) for webhook in webhooks]
    if [] in found:
        return Line("ok", check, "listed, active, with a secret and every handled event")
    return Line("warning", check, f"the webhook {'; '.join(found[0])}")


def _webhook_problems(webhook: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if webhook.get("active") is not True or webhook.get("disabled_at"):
        problems.append("is disabled: Razorpay sends it nothing")
    events: object = webhook.get("events")
    ticked = cast(dict[str, Any], events) if isinstance(events, dict) else {}
    lacking = sorted(event for event in HANDLED_TYPES if ticked.get(event) is not True)
    if lacking:
        problems.append(f"lacks a handled event: {', '.join(lacking)}")
    if webhook.get("secret_exists") is False:
        problems.append("has no secret: its deliveries can't be verified")
    return problems


def _same_url(listed: object, url: str) -> bool:
    return isinstance(listed, str) and listed.rstrip("/") == url.rstrip("/")


# -- the sheet -------------------------------------------------------------------------------------


async def _sheet(settings: Settings | None, service_account_b64: str) -> Line:
    check = "sheet"
    if settings is None:
        return Line("fail", check, "not checked: the config file must load first")
    try:
        key = service_account_key(service_account_b64)
    except ConfigError:
        return Line("fail", check, "not checked: GOOGLE_SERVICE_ACCOUNT_B64 must hold a key first")
    account = f"the service account {key.get('client_email', '')}".rstrip()
    try:
        stored = await asyncio.to_thread(stored_tabs, settings)
    except sqlite3.Error:
        stored = None  # the database line says what is wrong with it
    async with httpx.AsyncClient() as http:
        client = SheetsClient(http, settings.sheets.spreadsheet_id, ServiceAccountTokens(key))
        layout = await read_layout(client, settings, stored)
    if isinstance(layout, SheetsFailure):
        return Line("fail", check, sheet_failure(layout, account))
    return _layout(layout, account)


def _layout(layout: Layout, account: str) -> Line:
    if layout.problems:
        return Line("fail", "sheet", "; ".join(layout.problems))
    reached = f"reachable with {account}"
    if not layout.tabs:
        return Line("ok", "sheet", f"{reached}; no tabs of ours yet: the first flush makes them")
    return Line("ok", "sheet", f"{reached}; the tabs and their headers match")


def sheet_failure(failure: SheetsFailure, account: str) -> str:
    """A failed read in plain words, with whom to ask when an organisation's policy is the
    likely cause (Q90, Q101)."""
    if failure.error == KEY_REFUSED:
        return (
            f"Google refused the key of {account}: the key was deleted or disabled, or the "
            "Google organisation blocks service-account keys (the default for organisations "
            "created since May 2024); make a new key, or ask the organisation's admin to allow "
            "keys for this project"
        )
    if failure.http_status == 403:
        return (
            f"the spreadsheet is not shared with {account}: share it with that address as an "
            "Editor. If Google won't allow that, the Workspace blocks sharing outside its "
            "domain: ask its admin to allow it"
        )
    if failure.error == SPREADSHEET_MISSING:
        return (
            "no spreadsheet has the id in sinks.sheets.spreadsheet_id: it is mistyped, or the "
            "spreadsheet was deleted"
        )
    return f"the spreadsheet could not be read with {account}: {failure.error}"


def stored_tabs(settings: Settings) -> TabIds | None:
    """The tab ids a flush stored for the configured spreadsheet, or None before the first."""
    path = database_path(settings.service.data_dir)
    if not path.is_file():
        return None
    with closing(_connect(path, "ro")) as db:
        if not _has_table(db, "sheet_state"):
            return None
        row = db.execute(
            "SELECT payments_tab_id, daily_summary_tab_id, sync_status_tab_id FROM sheet_state"
            " WHERE spreadsheet_id = ?",
            (settings.sheets.spreadsheet_id,),
        ).fetchone()
    if row is None or None in row:
        return None
    return TabIds(*row)


# -- the database ----------------------------------------------------------------------------------


def _database(config_path: Path, settings: Settings | None) -> Line:
    check = "database"
    try:
        data_dir = settings.service.data_dir if settings else locate_data_dir(config_path)
    except ConfigError as error:
        return Line("fail", check, f"not checked: {error}")
    problems: list[str] = []
    elsewhere = not_local(data_dir)
    if elsewhere is not None:
        problems.append(f"{data_dir} is {elsewhere}, where SQLite's WAL is unsafe: move it")
    path = database_path(data_dir)
    if not path.is_file():
        if problems:
            return Line("fail", check, "; ".join(problems))
        return Line(
            "warning", check, f"no database in {data_dir} yet: wsync serve makes it at its start"
        )
    writable = os.access(path, os.W_OK) and os.access(data_dir, os.W_OK)
    if not writable:
        problems.append("the database file or its directory is read-only")
    version = 0
    try:
        with closing(_connect(path, "rw" if writable else "ro")) as db:
            if writable:
                # The write lock taken and given back: nothing is written.
                db.execute("BEGIN IMMEDIATE")
                db.execute("ROLLBACK")
            if _has_table(db, "schema_version"):
                version = db.execute("SELECT max(version) FROM schema_version").fetchone()[0] or 0
            journal = str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            # `synchronous` is this connection's own: set as the writer sets it, then read.
            db.execute("PRAGMA synchronous=FULL")
            synchronous = db.execute("PRAGMA synchronous").fetchone()[0]
    except sqlite3.Error as error:
        problems.append(f"the database can't be opened for writing: {error}")
        return Line("fail", check, "; ".join(problems))
    migrations = known_migrations()
    latest = migrations[-1].version if migrations else 0
    if version < latest:
        problems.append(
            f"{latest - version} migration(s) pending (schema {version}, this code's is "
            f"{latest}): start wsync serve once to apply them"
        )
    elif version > latest:
        problems.append(
            f"its schema {version} is newer than this code's {latest}: run the wsync that "
            "migrated it, or a newer one"
        )
    if journal != "wal":
        problems.append(f"its journal mode is {journal}, not WAL")
    if synchronous != 2:
        problems.append("synchronous=FULL does not take effect")
    if problems:
        return Line("fail", check, "; ".join(problems))
    return Line(
        "ok",
        check,
        f"writable, migrated (schema {version}), WAL with synchronous=FULL, on a local disk",
    )


def _connect(path: Path, mode: Literal["ro", "rw"]) -> sqlite3.Connection:
    """A connection that never creates the file, in autocommit: no statement opens a
    transaction of its own."""
    return sqlite3.connect(
        f"{path.resolve().as_uri()}?mode={mode}", uri=True, isolation_level=None, timeout=5
    )


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    found = db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,))
    return found.fetchone() is not None


SYNCED_FOLDERS = (
    "onedrive",
    "dropbox",
    "google drive",
    "googledrive",
    "my drive",
    "icloud drive",
    "iclouddrive",
    "mobile documents",
    "cloudstorage",
)
"""How a folder a sync client owns starts its name, lower case ("OneDrive - Company",
"Dropbox (Personal)"); the last two are where macOS keeps iCloud and the others."""

NETWORK_FILESYSTEMS = ("nfs", "cifs", "smb", "afs", "ceph", "gluster", "9p", "ncp", "davfs")
REMOTE_FUSE = ("sshfs", "rclone", "davfs", "s3fs", "gcsfuse", "onedrive", "gdfuse")
DRIVE_REMOTE = 4


def not_local(data_dir: Path) -> str | None:
    """What `data_dir` is on when it is not a local disk: "in a synced folder (OneDrive)" or
    "on a network drive (nfs4)"; None for a local disk."""
    resolved = data_dir.resolve()
    for part in resolved.parts:
        if part.lower().startswith(SYNCED_FOLDERS):
            return f"in a synced folder ({part})"
    if sys.platform == "win32":
        import ctypes

        if resolved.drive.startswith("\\\\"):
            return f"on a network drive ({resolved.drive})"
        if ctypes.windll.kernel32.GetDriveTypeW(resolved.anchor) == DRIVE_REMOTE:
            return f"on a network drive ({resolved.drive})"
        return None
    try:
        mounts = Path("/proc/self/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None  # no mount table to read (macOS): the folder names above are the check
    kind = network_filesystem(resolved, mounts)
    return f"on a network drive ({kind})" if kind is not None else None


def network_filesystem(path: PurePath, mounts: str) -> str | None:
    """The filesystem type of the mount holding `path` when it is a network one, read from a
    mount table in `/proc/mounts` form; None for a local one."""
    best, kind = "", ""
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        mount = fields[1].replace("\\040", " ")
        inside = mount == "/" or path.as_posix() == mount or path.as_posix().startswith(mount + "/")
        if inside and len(mount) >= len(best):
            best, kind = mount, fields[2].lower()
    remote = kind.startswith(NETWORK_FILESYSTEMS) or (
        kind.startswith("fuse") and any(name in kind for name in REMOTE_FUSE)
    )
    return kind if remote else None


# -- the public URL --------------------------------------------------------------------------------


async def _public_url(url: str) -> Line:
    """An empty POST, as a delivery would arrive: only the receiver's own 400 passes (Q112)."""
    check = "public url"
    seconds = URL_TIMEOUT.total_seconds()
    try:
        async with httpx.AsyncClient(timeout=seconds, follow_redirects=False) as http:
            response = await http.post(
                url, content=b"", headers={"content-type": "application/json"}
            )
    except httpx.TimeoutException:
        return Line("fail", check, f"no answer within {seconds:.0f} s: is the tunnel up?")
    except httpx.UnsupportedProtocol, httpx.InvalidURL:
        return Line("fail", check, "not a URL: it must start http:// or https://")
    except httpx.TransportError as error:
        host = httpx.URL(url).host
        if not await _resolves(host):
            return Line("fail", check, f"the name {host} does not resolve (DNS): is the tunnel up?")
        return Line("fail", check, f"could not connect ({type(error).__name__}): is the tunnel up?")
    status = response.status_code
    if response.is_redirect:
        return Line(
            "fail",
            check,
            f"answered a redirect (HTTP {status}), not the receiver: give Razorpay the URL it "
            "ends at",
        )
    content_type = response.headers.get("content-type", "").lower()
    if "html" in content_type or response.text.lstrip()[:1] == "<":
        if response.is_success:
            return Line(
                "fail",
                check,
                f"answered HTTP {status} with an HTML page, not the receiver: a tunnel's "
                "warning page, which would swallow real deliveries (Razorpay counts a 200 as "
                "delivered and never retries)",
            )
        return Line(
            "fail",
            check,
            f"answered HTTP {status} with an HTML page, not the receiver: the tunnel answers, "
            "the server behind it does not",
        )
    try:
        body: object = response.json()
    except ValueError:
        body = None
    rejection: object = (
        cast(dict[str, Any], body).get("rejection") if isinstance(body, dict) else None
    )
    if status == 400 and isinstance(rejection, str):
        return Line("ok", check, f"the receiver answered its own 400 ({rejection})")
    return Line(
        "fail",
        check,
        f"answered HTTP {status}, not the receiver's 400: the URL must end /webhooks/razorpay",
    )


async def _resolves(host: str) -> bool:
    """Whether the name has an address: asked again after a failed connection, since the error
    itself doesn't reliably say that the lookup is what failed."""
    try:
        await asyncio.get_running_loop().getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return True
