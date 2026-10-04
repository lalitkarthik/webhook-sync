"""Arming a crash point (Q159): `WSYNC_CRASH_AT=<name>`, read once by `wsync serve` at start.

It is refused with a key that is not a test-mode key, so it can't take a buyer's server down, and
refused for a name the server doesn't know, so a typo can't make a crash test pass by crashing
nowhere. Armed, the server says so in its log. Seam: the real `wsync serve` as a subprocess.
"""

import json
import subprocess
from pathlib import Path

from processes import (
    fixture_environment,
    free_port,
    stop,
    wait_until_healthy,
    write_serve_config,
    wsync_path,
)

from webhook_sync.cli.serve import EXIT_REFUSED

LIVE_KEY_ID = "rzp_live_fixtureKeyId01"


def serve_to_exit(config_path: Path, environ: dict[str, str]) -> tuple[int, str]:
    """Run `wsync serve` expecting it to stop by itself; its exit code and all its output."""
    result = subprocess.run(
        [wsync_path(), "serve", "--config", str(config_path)],
        env=environ,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def test_a_crash_point_with_a_live_key_refuses_to_start_and_says_why(tmp_path: Path) -> None:
    config_path = write_serve_config(tmp_path / "conf", free_port())
    live = config_path.read_text(encoding="utf-8").replace('mode = "test"', 'mode = "live"')
    config_path.write_text(live, encoding="utf-8")
    environ = fixture_environment() | {
        "RAZORPAY_KEY_ID": LIVE_KEY_ID,
        "WSYNC_CRASH_AT": "after_event_commit",
    }

    code, output = serve_to_exit(config_path, environ)

    assert (code, output.strip()) == (
        EXIT_REFUSED,
        "wsync serve: WSYNC_CRASH_AT needs a test-mode key: RAZORPAY_KEY_ID must start rzp_test_",
    )
    assert LIVE_KEY_ID not in output


def test_an_unknown_crash_point_refuses_to_start_and_lists_the_names_it_knows(
    tmp_path: Path,
) -> None:
    config_path = write_serve_config(tmp_path / "conf", free_port())
    environ = fixture_environment() | {"WSYNC_CRASH_AT": "after_event_comit"}

    code, output = serve_to_exit(config_path, environ)

    assert (code, output.strip()) == (
        EXIT_REFUSED,
        "wsync serve: WSYNC_CRASH_AT names no crash point the server knows:"
        " 'after_event_comit'; it knows after_event_commit",
    )


def test_an_armed_crash_point_logs_one_warning_naming_it_at_start(tmp_path: Path) -> None:
    port = free_port()
    config_path = write_serve_config(tmp_path / "conf", port)
    environ = fixture_environment() | {"WSYNC_CRASH_AT": "after_event_commit"}
    log_path = tmp_path / "serve.log"

    with log_path.open("wb") as log_file:
        process = subprocess.Popen(
            [wsync_path(), "serve", "--config", str(config_path)],
            env=environ,
            stdout=log_file,
            stderr=log_file,
        )
        try:
            wait_until_healthy(f"http://127.0.0.1:{port}", process)
        finally:
            stop(process)

    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    warnings = [e["message"] for e in entries if e["level"] == "warning"]
    assert warnings == [
        "crash point armed: the server exits at after_event_commit without cleaning up"
    ]
