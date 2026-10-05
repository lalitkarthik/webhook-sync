"""`wsync doctor` as a real subprocess (#60): the exit code and the printed lines, with a server
running and without one. Razorpay and Google are the fakes over HTTP on 127.0.0.1, so nothing
leaves this machine; the public URL is the running server's own receiver.
"""

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from fakes.over_http import FakesOverHttp
from fakes.razorpay import FakeRazorpay, listed_webhook
from fakes.sheets import FakeSheets
from processes import (
    fixture_environment,
    free_port,
    wait_until_healthy,
    write_serve_config,
    wsync_path,
)
from support import RECORDED_ACCOUNT, TEST_SPREADSHEET_ID

QUIET_SERVER = '[sweeper]\ninitial_lookback_days = 0\n\n[verify]\nnightly_at = "23:59"\n'
CHECKS = ["config", "secrets", "razorpay key", "razorpay webhook", "clock", "sheet", "database"]


def wsync(environ: dict[str, str], *args: str) -> tuple[int, list[str], str]:
    result = subprocess.run(
        [wsync_path(), *args],
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.returncode, result.stdout.splitlines(), result.stderr


def check_of(line: str) -> str:
    return line.partition(" ")[2].strip().partition(":")[0]


def test_with_a_server_running_doctor_passes_every_check_and_its_own_receivers_400(
    tmp_path: Path,
) -> None:
    port = free_port()
    url = f"http://127.0.0.1:{port}/webhooks/razorpay"
    razorpay = FakeRazorpay(now=datetime.now(UTC), webhooks=[listed_webhook(url)])
    with FakesOverHttp(razorpay, FakeSheets(TEST_SPREADSHEET_ID)) as fakes:
        config_path = write_serve_config(
            tmp_path / "conf", port, account=RECORDED_ACCOUNT, api_url=fakes.url, extra=QUIET_SERVER
        )
        environ = fakes.environ(fixture_environment())
        server = subprocess.Popen(
            [wsync_path(), "serve", "--config", str(config_path)],
            env=environ,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            wait_until_healthy(f"http://127.0.0.1:{port}", server)
            code, lines, err = wsync(environ, "doctor", "--config", str(config_path), "--url", url)
            still_serving = server.poll() is None
        finally:
            server.kill()
            server.wait()

    assert code == 0, (lines, err)
    assert [check_of(line) for line in lines] == [*CHECKS, "public url"]
    assert all(line.startswith("ok ") for line in lines), lines
    assert lines[-1] == "ok      public url: the receiver answered its own 400 (bad_signature)"
    assert still_serving


def test_without_a_server_a_missing_secret_is_a_fail_line_and_exit_code_1(tmp_path: Path) -> None:
    razorpay = FakeRazorpay(now=datetime.now(UTC))
    with FakesOverHttp(razorpay, FakeSheets(TEST_SPREADSHEET_ID)) as fakes:
        config_path = write_serve_config(
            tmp_path / "conf", free_port(), account=RECORDED_ACCOUNT, api_url=fakes.url
        )
        environ = fakes.environ(fixture_environment())
        del environ["RAZORPAY_WEBHOOK_SECRET"]
        code, lines, _ = wsync(environ, "doctor", "--config", str(config_path))

    assert code == 1
    assert [check_of(line) for line in lines] == CHECKS
    assert lines[1] == "fail    secrets: RAZORPAY_WEBHOOK_SECRET is missing or empty"
    states = {check_of(line): line.split()[0] for line in lines}
    assert states == dict.fromkeys(CHECKS, "ok") | {"secrets": "fail", "database": "warning"}
