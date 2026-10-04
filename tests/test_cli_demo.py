"""`wsync demo reset`, `outage` and `verify` as real subprocesses (seam 3; Q105, Q120, Q181).

Razorpay and Google are FakeRazorpay and FakeSheets over HTTP on 127.0.0.1, as in the command
tests of P4 and the crash tests of P5, and the payer is a fake one (`fakes.payer`) the command
loads by `WSYNC_DEMO_PAYER`: it pays an order through FakeRazorpay's checkout call, where the
real payer drives a browser. What comes out is what the owner sees: the printed lines, the exit
code, the ledger, the sheet and the config file.
"""

import subprocess
from pathlib import Path

import pytest
from processes import fixture_environment, free_port, write_serve_config, wsync_path
from support import (
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_SPREADSHEET_ID,
    TEST_WEBHOOK_SECRET,
)

SCENES = ("reset", "outage", "verify")
LIVE_KEY_ID = "rzp_live_fixtureKeyId01"
SECRET_VALUES = (TEST_KEY_ID, TEST_KEY_SECRET, TEST_WEBHOOK_SECRET, LIVE_KEY_ID)
ON_THE_DEMO_LIST = f'\n[demo]\nspreadsheet_ids = ["{TEST_SPREADSHEET_ID}"]\n'


def demo(config_path: Path, scene: str, environ: dict[str, str]) -> tuple[int, str]:
    result = subprocess.run(
        [wsync_path(), "demo", scene, "--config", str(config_path)],
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = result.stdout + result.stderr
    assert [value for value in SECRET_VALUES if value in output] == []
    return result.returncode, output


@pytest.mark.parametrize("scene", SCENES)
def test_every_scene_refuses_a_key_that_is_not_a_test_key(tmp_path: Path, scene: str) -> None:
    config_path = write_serve_config(tmp_path, free_port(), extra=ON_THE_DEMO_LIST)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace('mode = "test"', 'mode = "live"'),
        encoding="utf-8",
    )
    environ = fixture_environment() | {"RAZORPAY_KEY_ID": LIVE_KEY_ID}

    code, output = demo(config_path, scene, environ)

    assert code == 2
    assert f"wsync demo {scene}: wsync demo needs a test-mode key" in output
    assert "RAZORPAY_KEY_ID must start rzp_test_" in output
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("scene", SCENES)
@pytest.mark.parametrize("listed", ["", '\n[demo]\nspreadsheet_ids = ["another-sheet-0002"]\n'])
def test_every_scene_refuses_a_sheet_not_on_the_demo_list(
    tmp_path: Path, scene: str, listed: str
) -> None:
    config_path = write_serve_config(tmp_path, free_port(), extra=listed)

    code, output = demo(config_path, scene, fixture_environment())

    assert code == 2
    assert f"wsync demo {scene}: wsync demo needs sinks.sheets.spreadsheet_id listed in" in output
    assert "demo.spreadsheet_ids" in output
    assert not (tmp_path / "data").exists()
