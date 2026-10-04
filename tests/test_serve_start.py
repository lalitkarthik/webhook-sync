"""Fail-closed start as a real subprocess (seam 3): the lock, the schema check, exit codes.

Each refusal is observed as an outsider sees it: a non-zero exit code and a first line of output
naming what is wrong, never a secret's value.
"""

import signal
import sqlite3
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest
from processes import (
    fixture_environment,
    free_port,
    wait_until_healthy,
    write_serve_config,
    wsync_path,
)
from support import TEST_KEY_ID, TEST_KEY_SECRET, TEST_WEBHOOK_SECRET


def serve_to_exit(config_path: Path, environ: dict[str, str] | None = None) -> tuple[int, str]:
    """Run `wsync serve` expecting it to stop by itself; its exit code and all its output."""
    result = subprocess.run(
        [wsync_path(), "serve", "--config", str(config_path)],
        env=fixture_environment() if environ is None else environ,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    for secret in (TEST_KEY_ID, TEST_KEY_SECRET, TEST_WEBHOOK_SECRET):
        assert secret not in output
    return result.returncode, output


def first_line(output: str) -> str:
    return output.splitlines()[0] if output else ""


@contextmanager
def running_server(config_path: Path, port: int) -> Generator[subprocess.Popen[bytes]]:
    """`wsync serve` started and healthy; stopped with SIGTERM at the end unless already dead."""
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
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


@pytest.mark.posix
def test_a_second_server_is_refused_while_the_first_runs_and_starts_once_it_is_killed(
    tmp_path: Path,
) -> None:
    port = free_port()
    config_path = write_serve_config(tmp_path / "conf", port)

    with running_server(config_path, port) as first:
        code, output = serve_to_exit(config_path)
        first.send_signal(signal.SIGKILL)
        first.wait()

    assert code != 0
    assert "webhook-sync.lock" in first_line(output)
    with running_server(config_path, port) as after_the_kill:
        assert after_the_kill.poll() is None


def test_a_database_with_a_newer_schema_is_refused_and_left_untouched(tmp_path: Path) -> None:
    config_path = write_serve_config(tmp_path / "conf", free_port())
    data_dir = tmp_path / "conf" / "data"
    data_dir.mkdir()
    with sqlite3.connect(data_dir / "webhook-sync.db") as db:
        db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
        db.execute("INSERT INTO schema_version VALUES (9999, '2027-01-01T00:00:00.000000Z')")
    db.close()

    code, output = serve_to_exit(config_path)

    assert code != 0
    assert "schema_version" in first_line(output)
    with sqlite3.connect(data_dir / "webhook-sync.db") as db:
        tables = db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    db.close()
    assert tables == [("schema_version",)]


@pytest.mark.posix
def test_migrations_apply_once_across_two_starts(tmp_path: Path) -> None:
    port = free_port()
    config_path = write_serve_config(tmp_path / "conf", port)

    for _ in range(2):
        with running_server(config_path, port):
            pass

    with sqlite3.connect(tmp_path / "conf" / "data" / "webhook-sync.db") as db:
        versions = db.execute("SELECT version FROM schema_version").fetchall()
    db.close()
    assert versions == [(1,), (2,), (3,), (4,)]


def test_a_missing_secret_exits_non_zero_naming_the_variable(tmp_path: Path) -> None:
    config_path = write_serve_config(tmp_path / "conf", free_port())
    environ = fixture_environment()
    del environ["RAZORPAY_WEBHOOK_SECRET"]

    code, output = serve_to_exit(config_path, environ)

    assert code != 0
    assert "RAZORPAY_WEBHOOK_SECRET" in first_line(output)


def test_an_unknown_key_exits_non_zero_naming_the_key(tmp_path: Path) -> None:
    config_path = write_serve_config(tmp_path / "conf", free_port())
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + "\n[alerts]\nurll = ''\n", encoding="utf-8"
    )

    code, output = serve_to_exit(config_path)

    assert code != 0
    assert "alerts.urll" in first_line(output)


def test_a_missing_config_file_exits_non_zero_naming_the_default_path(tmp_path: Path) -> None:
    result = subprocess.run(
        [wsync_path(), "serve"],
        cwd=tmp_path,
        env=fixture_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "webhook-sync.toml" in first_line(result.stderr)
