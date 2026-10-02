"""The demo fail-first switch at start (seam 3): refused unless test mode and an allow-listed sheet.

In process through `start` for the refusals, and as a real `wsync serve --demo-fail-first N`
subprocess for the exit code and the 500s an outsider sees (A6, Q120).
"""

import signal
import subprocess
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
    TEST_SPREADSHEET_ID,
    TEST_WEBHOOK_SECRET,
    fixture_secrets,
    razorpay_sample,
    sign_razorpay,
)

from webhook_sync.cli.serve import StartRefused, start

LIVE_KEY_ID = "rzp_live_fixtureKeyId01"
SECRET_VALUES = (TEST_KEY_ID, TEST_KEY_SECRET, TEST_WEBHOOK_SECRET, LIVE_KEY_ID)


def write_toml(directory: Path, *, mode: str = "test", demo_ids: tuple[str, ...] = ()) -> Path:
    listed = ", ".join(f'"{i}"' for i in demo_ids)
    path = directory / "webhook-sync.toml"
    path.write_text(
        f'[service]\ndata_dir = "data"\n\n'
        f'[sources.razorpay]\nmode = "{mode}"\naccount = "{TEST_ACCOUNT}"\n\n'
        f'[sinks.sheets]\nspreadsheet_id = "{TEST_SPREADSHEET_ID}"\n\n'
        f"[demo]\nspreadsheet_ids = [{listed}]\n",
        encoding="utf-8",
    )
    return path


def live_secrets() -> dict[str, str]:
    return fixture_secrets() | {"RAZORPAY_KEY_ID": LIVE_KEY_ID}


def demo_refusal(path: Path, environ: dict[str, str]) -> str:
    with pytest.raises(StartRefused) as refused, start(path, environ, demo_fail_first=4):
        pass
    line = str(refused.value)
    assert "\n" not in line
    assert [value for value in SECRET_VALUES if value in line] == []
    return line


def test_the_switch_with_a_live_key_is_refused_naming_the_key_variable(tmp_path: Path) -> None:
    path = write_toml(tmp_path, mode="live", demo_ids=(TEST_SPREADSHEET_ID,))

    line = demo_refusal(path, live_secrets())

    assert "--demo-fail-first" in line
    assert "RAZORPAY_KEY_ID" in line
    assert "rzp_test_" in line


@pytest.mark.parametrize("demo_ids", [(), ("another-sheet-0002",)])
def test_the_switch_with_a_sheet_not_on_the_demo_list_is_refused(
    tmp_path: Path, demo_ids: tuple[str, ...]
) -> None:
    path = write_toml(tmp_path, demo_ids=demo_ids)

    line = demo_refusal(path, fixture_secrets())

    assert "--demo-fail-first" in line
    assert "demo.spreadsheet_ids" in line


def test_the_switch_starts_in_test_mode_with_an_allow_listed_sheet(tmp_path: Path) -> None:
    path = write_toml(tmp_path, demo_ids=("another-sheet-0002", TEST_SPREADSHEET_ID))

    with start(path, fixture_secrets(), demo_fail_first=4) as config:
        assert config.demo.spreadsheet_ids == ("another-sheet-0002", TEST_SPREADSHEET_ID)


def test_without_the_switch_a_live_config_off_the_demo_list_still_starts(tmp_path: Path) -> None:
    path = write_toml(tmp_path, mode="live")

    with start(path, live_secrets()) as config:
        assert config.razorpay.mode == "live"


def serve_demo_to_exit(config_path: Path, *args: str) -> tuple[int, str]:
    result = subprocess.run(
        [wsync_path(), "serve", "--config", str(config_path), *args],
        env=fixture_environment(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    assert [value for value in SECRET_VALUES if value in output] == []
    return result.returncode, output


def test_wsync_serve_refuses_the_switch_off_the_demo_list_with_one_line(tmp_path: Path) -> None:
    config_path = write_serve_config(tmp_path / "conf", free_port())

    code, output = serve_demo_to_exit(config_path, "--demo-fail-first", "4")

    assert code != 0
    assert len(output.splitlines()) == 1
    assert "demo.spreadsheet_ids" in output


@pytest.mark.parametrize("n", ["0", "-1", "four"])
def test_wsync_serve_refuses_an_n_below_one(tmp_path: Path, n: str) -> None:
    config_path = write_serve_config(tmp_path / "conf", free_port())

    code, output = serve_demo_to_exit(config_path, f"--demo-fail-first={n}")

    assert code != 0
    assert "--demo-fail-first" in output


@pytest.mark.unrecorded
def test_wsync_serve_with_the_switch_answers_500_twice_then_the_duplicate(tmp_path: Path) -> None:
    port = free_port()
    config_path = write_serve_config(tmp_path / "conf", port)
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + f'\n[demo]\nspreadsheet_ids = ["{TEST_SPREADSHEET_ID}"]\n',
        encoding="utf-8",
    )
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_storm02")
    process = subprocess.Popen(
        [wsync_path(), "serve", "--config", str(config_path), "--demo-fail-first", "2"],
        env=fixture_environment(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        base_url = f"http://127.0.0.1:{port}"
        wait_until_healthy(base_url, process)
        statuses = [
            httpx.post(f"{base_url}/webhooks/razorpay", content=body, headers=headers).status_code
            for _ in range(3)
        ]
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=10)

    assert statuses == [500, 500, 200]
