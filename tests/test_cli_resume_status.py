"""`wsync resume` and `wsync status` as real subprocesses (seam 3), with a server running (through
the admin listener) and without one (on the database); and `wsync serve`'s P3 refusals.
"""

import signal
import sqlite3
import subprocess
import time
from collections.abc import Generator
from contextlib import closing, contextmanager
from pathlib import Path

import httpx
import pytest
from processes import (
    fixture_environment,
    free_port,
    wait_until_healthy,
    write_serve_config,
    wsync_path,
)
from support import (
    TEST_ACCOUNT,
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_WEBHOOK_SECRET,
    fixture_secrets,
    fixture_service_account_b64,
)

from webhook_sync.cli.serve import start

PAUSED_AT = "2026-10-02T19:00:00.000000Z"
RAZORPAY_PAUSED = f"razorpay (source): paused since {PAUSED_AT}: Razorpay answered HTTP 401"


def wsync(*args: str, environ: dict[str, str] | None = None) -> tuple[int, str, str]:
    result = subprocess.run(
        [wsync_path(), *args],
        env=fixture_environment() if environ is None else environ,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode, result.stdout, result.stderr


def database(config_path: Path) -> Path:
    return config_path.parent / "data" / "webhook-sync.db"


def run_sql(config_path: Path, *statements: str) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(database(config_path))) as db, db:
        rows: list[tuple[object, ...]] = []
        for statement in statements:
            rows = db.execute(statement).fetchall()
        return rows


def pause_razorpay_with_jobs(config_path: Path) -> None:
    """A database migrated by a start, Razorpay paused by a 401, and jobs of both stages."""
    with start(config_path, fixture_secrets()):
        pass
    jobs = [("ledger", "ledger", "waiting"), ("ledger", "ledger", "dead")]
    jobs += [("mirror", "sheets", "waiting")] * 2
    run_sql(
        config_path,
        "UPDATE sinks SET state = 'paused', reason = 'Razorpay answered HTTP 401',"
        f" since = '{PAUSED_AT}' WHERE name = 'razorpay'",
        *(
            "INSERT INTO jobs (stage, sink, source, account, payment_key, status, rerun,"
            " attempts, max_attempts, run_after, created_at, updated_at) VALUES"
            f" ('{stage}', '{sink}', 'razorpay', '{TEST_ACCOUNT}', 'pay_{i}', '{status}', 0, 0,"
            f" 8, '{PAUSED_AT}', '{PAUSED_AT}', '{PAUSED_AT}')"
            for i, (stage, sink, status) in enumerate(jobs)
        ),
    )


def ledger_attempts(config_path: Path) -> int:
    [(attempts,)] = run_sql(
        config_path, "SELECT attempts FROM jobs WHERE stage = 'ledger' AND status != 'dead'"
    )
    return int(str(attempts))


@contextmanager
def serving(config_path: Path, port: int) -> Generator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(
        [wsync_path(), "serve", "--config", str(config_path)],
        env=fixture_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_until_healthy(f"http://127.0.0.1:{port}", process)
        yield process
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_without_a_server_status_reads_and_resume_updates_the_database(tmp_path: Path) -> None:
    config_path = write_serve_config(tmp_path / "conf", free_port())
    pause_razorpay_with_jobs(config_path)

    status_code, status, _ = wsync("status", "--config", str(config_path))
    resume_code, resumed, resume_log = wsync("resume", "razorpay", "--config", str(config_path))
    again_code, again, _ = wsync("resume", "razorpay", "--config", str(config_path))
    _, after, _ = wsync("status", "--config", str(config_path))

    assert status_code == 0
    assert status.splitlines() == [
        "server: on the database: no server is running",
        RAZORPAY_PAUSED,
        "sheets (sink): active",
        "jobs: ledger 1 waiting, 1 dead; mirror 2 waiting, 0 dead",
        "last flush: never",
    ]
    assert resume_code == 0
    assert resumed.strip() == (
        "razorpay resumed: claims start again (on the database: no server is running)"
    )
    assert '"message": "pause cleared"' in resume_log
    assert '"by": "cli"' in resume_log
    assert (again_code, again.strip()) == (
        0,
        "razorpay is not paused (on the database: no server is running)",
    )
    assert "razorpay (source): active" in after.splitlines()
    assert run_sql(config_path, "SELECT state, reason FROM sinks WHERE name = 'razorpay'") == [
        ("active", None)
    ]


def test_resume_of_an_unknown_name_fails_and_says_so(tmp_path: Path) -> None:
    config_path = write_serve_config(tmp_path / "conf", free_port())
    pause_razorpay_with_jobs(config_path)

    code, _, error = wsync("resume", "stripe", "--config", str(config_path))

    assert code == 1
    assert "nothing is named 'stripe'" in error


def test_status_before_any_start_says_there_is_no_database(tmp_path: Path) -> None:
    config_path = write_serve_config(tmp_path / "conf", free_port())

    code, _, error = wsync("status", "--config", str(config_path))

    assert code == 1
    assert "no database" in error


def test_with_a_server_running_the_pause_holds_until_resume_through_the_admin_listener(
    tmp_path: Path,
) -> None:
    port, admin_port = free_port(), free_port()
    config_path = write_serve_config(tmp_path / "conf", port, admin_port)
    pause_razorpay_with_jobs(config_path)
    config = str(config_path)

    with serving(config_path, port) as server:
        _, status, _ = wsync("status", "--config", config)
        time.sleep(1.5)  # more than the runner's idle wait: it would have claimed by now
        attempts_while_paused = ledger_attempts(config_path)
        resume_code, resumed, _ = wsync("resume", "razorpay", "--config", config)
        deadline = time.monotonic() + 10
        while ledger_attempts(config_path) == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        attempts_after_resume = ledger_attempts(config_path)
        _, after, _ = wsync("status", "--config", config)
        server.send_signal(signal.SIGTERM)
        exit_code = server.wait(timeout=30)

    assert status.splitlines()[:3] == [
        "server: through the running server",
        RAZORPAY_PAUSED,
        "sheets (sink): active",
    ]
    assert "jobs: ledger 1 waiting, 1 dead; mirror 2 waiting, 0 dead" in status.splitlines()
    assert attempts_while_paused == 0
    assert (resume_code, resumed.strip()) == (
        0,
        "razorpay resumed: claims start again (through the running server)",
    )
    assert attempts_after_resume == 1
    assert "razorpay (source): active" in after.splitlines()
    assert exit_code == 0
    with pytest.raises(httpx.TransportError):
        httpx.get(f"http://127.0.0.1:{admin_port}/admin/status", timeout=1)
    _, offline, _ = wsync("status", "--config", config)
    assert offline.splitlines()[0] == "server: on the database: no server is running"


@pytest.mark.parametrize(
    ("toml_extra", "environ_change", "named"),
    [
        ('admin_listen = "0.0.0.0:8081"', {}, "service.admin_listen"),
        ("", {"GOOGLE_SERVICE_ACCOUNT_B64": None}, "GOOGLE_SERVICE_ACCOUNT_B64"),
        (
            "",
            {"GOOGLE_SERVICE_ACCOUNT_B64": fixture_service_account_b64(type="authorized_user")},
            "GOOGLE_SERVICE_ACCOUNT_B64",
        ),
        ('columns = ["payment_id", "settled"]', {}, "sinks.sheets.columns"),
    ],
    ids=["non-loopback-admin", "missing-key", "not-a-service-account", "unknown-column"],
)
def test_serve_refuses_p3_settings_naming_them_and_never_a_secret(
    tmp_path: Path, toml_extra: str, environ_change: dict[str, str | None], named: str
) -> None:
    config_path = write_serve_config(tmp_path / "conf", free_port())
    text = config_path.read_text()
    if toml_extra.startswith("admin_listen"):
        text = "\n".join(
            toml_extra if line.startswith("admin_listen") else line for line in text.splitlines()
        )
    elif toml_extra:
        text += f"{toml_extra}\n"
    config_path.write_text(text)
    environ = fixture_environment()
    for name, value in environ_change.items():
        if value is None:
            del environ[name]
        else:
            environ[name] = value

    code, out, err = wsync("serve", "--config", str(config_path), environ=environ)

    assert code == 2
    assert named in (out + err).splitlines()[0]
    secrets = (TEST_KEY_ID, TEST_KEY_SECRET, TEST_WEBHOOK_SECRET, "PRIVATE KEY")
    assert [s for s in secrets if s in out + err] == []
    given = environ_change.get("GOOGLE_SERVICE_ACCOUNT_B64")
    assert given is None or given not in out + err
