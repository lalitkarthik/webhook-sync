"""`wsync sweep`, `wsync backfill` and `wsync verify` as real subprocesses (Q152, #43): through
the running server's admin listener when one holds the lock, polling the run until it ends; on
their own under the lock when none does. And `wsync status` showing what they did.

Razorpay is `tests/fakes/upstreams.py` on 127.0.0.1, its lists answering the recorded items moved
in time, and the service account's token endpoint is the same fake, so nothing leaves this
machine. What comes out is what an outsider sees: exit codes, output, the lock, and the
database's events and verify runs.
"""

import re
import sqlite3
import subprocess
import time
from collections.abc import Generator
from contextlib import closing, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from fakes.upstreams import list_calls, start_upstreams, token_uri
from processes import (
    fixture_environment,
    free_port,
    wait_until_healthy,
    write_serve_config,
    wsync_path,
)
from support import RECORDED_ACCOUNT, fixture_secrets, fixture_service_account_b64

from webhook_sync.cli.serve import start
from webhook_sync.db.lock import lock_if_free

LISTED = sorted(
    (
        "pay_Tj64grZUMO3PEj",
        "pay_Tj66x2JuEXjIdB",
        "pay_Tj9gmEfed6kRqO",
        "pay_Tj9rKBOXWcsMeE",
        "pay_Tj9sm13R8h2378",
    )
)
"""Every recorded payment but the two never attempted (`created`): what a list brings in."""
ON_ITS_OWN = "(on the database: no server is running)"
THROUGH_SERVER = "(through the running server)"
QUIET_SERVER = '[sweeper]\ninitial_lookback_days = 0\n\n[verify]\nnightly_at = "23:59"\n'
"""A server whose own sweep and nightly verify leave the recorded payments alone."""


@dataclass
class Setup:
    config_path: Path
    port: int
    admin_port: int
    upstreams: ThreadingHTTPServer
    environ: dict[str, str]

    @property
    def data_dir(self) -> Path:
        return self.config_path.parent / "data"

    def wsync(self, *args: str) -> tuple[int, str, str]:
        result = subprocess.run(
            [wsync_path(), *args, "--config", str(self.config_path)],
            env=self.environ,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return result.returncode, result.stdout, result.stderr

    def popen(self, *args: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [wsync_path(), *args, "--config", str(self.config_path)],
            env=self.environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def rows(self, sql: str) -> list[tuple[object, ...]]:
        with closing(sqlite3.connect(self.data_dir / "webhook-sync.db")) as db:
            return db.execute(sql).fetchall()


@contextmanager
def setup(
    tmp_path: Path, *, listed_at: datetime, list_delay: float = 0.0, extra: str = ""
) -> Generator[Setup]:
    """A database migrated by a start and the fakes running; nothing serving yet."""
    upstreams = start_upstreams(listed_at=listed_at, list_delay=list_delay)
    fakes_port = upstreams.server_address[1]
    port, admin_port = free_port(), free_port()
    config_path = write_serve_config(
        tmp_path / "conf",
        port,
        admin_port,
        account=RECORDED_ACCOUNT,
        api_url=f"http://127.0.0.1:{fakes_port}",
        extra=extra,
    )
    with start(config_path, fixture_secrets()):
        pass
    environ = fixture_environment() | {
        "GOOGLE_SERVICE_ACCOUNT_B64": fixture_service_account_b64(token_uri=token_uri(fakes_port))
    }
    try:
        yield Setup(config_path, port, admin_port, upstreams, environ)
    finally:
        upstreams.shutdown()
        upstreams.server_close()


@contextmanager
def serving(on: Setup) -> Generator[None]:
    with (on.config_path.parent / "serve.log").open("wb") as log_file:
        process = subprocess.Popen(
            [wsync_path(), "serve", "--config", str(on.config_path)],
            env=on.environ,
            stdout=log_file,
            stderr=log_file,
        )
        try:
            wait_until_healthy(f"http://127.0.0.1:{on.port}", process)
            yield
        finally:
            process.kill()
            process.wait()


def locked_while_listing(on: Setup, command: subprocess.Popen[str]) -> bool:
    """Whether the lock was held once the command had begun listing."""
    deadline = time.monotonic() + 30
    while not list_calls(on.upstreams) and time.monotonic() < deadline:
        assert command.poll() is None, "the command ended before it listed"
        time.sleep(0.01)
    with lock_if_free(on.data_dir) as free:
        return not free


def yesterday() -> datetime:
    return datetime.now(UTC) - timedelta(days=1)


def test_without_a_server_a_sweep_runs_on_its_own_under_the_lock_and_status_shows_it(
    tmp_path: Path,
) -> None:
    with setup(
        tmp_path,
        listed_at=yesterday(),
        list_delay=0.3,
        extra="[sweeper]\ninitial_lookback_days = 2\n",
    ) as on:
        command = on.popen("sweep")
        held = locked_while_listing(on, command)
        out, err = command.communicate(timeout=60)
        _, status, _ = on.wsync("status")
        stored = on.rows("SELECT origin, payment_key FROM events")
        with lock_if_free(on.data_dir) as free_after:
            pass

    assert command.returncode == 0, err
    assert held
    assert free_after
    lines = out.splitlines()
    assert lines[0] == f"sweep done {ON_ITS_OWN}"
    assert re.fullmatch(r"last sweep of payments: \S+, 5 stored; checkpoint \S+", lines[1])
    assert re.fullmatch(r"last sweep of refunds: \S+, 2 stored; checkpoint \S+", lines[2])
    assert {origin for origin, _ in stored} == {"sweep"}
    assert sorted({str(key) for _, key in stored}) == LISTED
    assert lines[1] in status.splitlines()
    assert lines[2] in status.splitlines()


def test_without_a_server_a_full_verify_runs_on_its_own_and_prints_its_findings(
    tmp_path: Path,
) -> None:
    with setup(tmp_path, listed_at=yesterday(), list_delay=0.5) as on:
        command = on.popen("verify", "--full")
        held = locked_while_listing(on, command)
        out, err = command.communicate(timeout=60)
        _, status, _ = on.wsync("status")
        runs = on.rows("SELECT kind, status_line FROM verify_runs")

    assert command.returncode == 0, err
    assert held
    lines = out.splitlines()
    assert re.fullmatch(
        r"Verified \d\d \w{3} \d\d:\d\d IST: last 30 days, 5 payments, 5 differences", lines[0]
    )
    assert lines[1] == f"5 findings, 0 repaired {ON_ITS_OWN}"
    assert sorted(lines[2:]) == [f"missing {payment_id}" for payment_id in LISTED]
    assert runs == [("full", lines[0])]
    assert f"last verify: {lines[0]}" in status.splitlines()


@pytest.mark.parametrize("with_server", [False, True], ids=["on-its-own", "through-server"])
def test_backfill_stores_backfill_events_for_the_local_dates_given(
    tmp_path: Path, with_server: bool
) -> None:
    with setup(
        tmp_path, listed_at=datetime(2026, 9, 5, 12, 0, tzinfo=UTC), extra=QUIET_SERVER
    ) as on:
        with serving(on) if with_server else nullcontext():
            code, out, err = on.wsync("backfill", "--from", "2026-09-01", "--to", "2026-09-10")
        stored = on.rows("SELECT event_id, origin, payment_key FROM events")

    assert code == 0, err
    way = THROUGH_SERVER if with_server else ON_ITS_OWN
    assert out.splitlines()[0] == f"backfill 2026-09-01 to 2026-09-10: 5 events stored {way}"
    assert {origin for _, origin, _ in stored} == {"backfill"}
    assert all(str(event_id).startswith(f"backfill:{key}:") for event_id, _, key in stored)
    assert sorted(str(key) for _, _, key in stored) == LISTED


def test_with_a_server_a_full_verify_longer_than_a_request_timeout_prints_its_findings(
    tmp_path: Path,
) -> None:
    slow = setup(tmp_path, listed_at=yesterday(), list_delay=6, extra=QUIET_SERVER)
    with slow as on, serving(on):
        began = time.monotonic()
        code, out, err = on.wsync("verify", "--full")
        took = time.monotonic() - began

    assert code == 0, err
    assert took > 10
    lines = out.splitlines()
    assert re.fullmatch(
        r"Verified \d\d \w{3} \d\d:\d\d IST: last 30 days, 5 payments, 5 differences", lines[0]
    )
    assert lines[1] == f"5 findings, 0 repaired {THROUGH_SERVER}"
    assert sorted(lines[2:]) == [f"missing {payment_id}" for payment_id in LISTED]


def test_with_a_server_verify_no_wait_prints_the_run_id_at_once(tmp_path: Path) -> None:
    with setup(tmp_path, listed_at=yesterday(), extra=QUIET_SERVER) as on, serving(on):
        code, out, err = on.wsync("verify", "--no-wait")
        run_id = out.split()[1] if out else ""
        report = httpx.get(f"http://127.0.0.1:{on.admin_port}/admin/runs/{run_id}")

    assert code == 0, err
    assert re.fullmatch(rf"run [0-9a-f]{{16}} started {re.escape(THROUGH_SERVER)}", out.strip())
    assert report.status_code == 200
    assert report.json()["kind"] == "verify"


@pytest.mark.parametrize(
    ("args", "named"),
    [
        (("verify", "--days", "3"), "--days"),
        (("backfill", "--from", "2026-09-10", "--to", "2026-09-01"), "--from"),
        (("backfill", "--from", "1 Sep", "--to", "2026-09-01"), "--from"),
    ],
    ids=["days-without-full", "dates-backwards", "not-a-date"],
)
def test_arguments_that_make_no_sense_are_refused_naming_them(
    tmp_path: Path, args: tuple[str, ...], named: str
) -> None:
    config_path = write_serve_config(tmp_path / "conf", free_port())

    result = subprocess.run(
        [wsync_path(), *args, "--config", str(config_path)],
        env=fixture_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 2
    assert named in result.stderr
