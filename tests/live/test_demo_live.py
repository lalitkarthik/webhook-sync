"""The live test (Q175, A2, A15): `wsync demo reset`, `outage` and `verify` against Razorpay test
mode and the throwaway sheet, with the test keys from `.env`, the headless payer paying.

Marked `live`, so only `uv run pytest -m live -s` runs it: the default suite never calls Razorpay
or Google. It reads the secrets from `WSYNC_ENV_FILE`, else the repository's `.env`, refuses a
key that is not a test key, and fails if any scene prints a secret's value or the sheet's id. It
prints each scene's lines, which the flow check quotes.
"""

import os
import subprocess
from pathlib import Path

import pytest
from dotenv import dotenv_values
from processes import free_port, wsync_path
from support import RECORDED_ACCOUNT

from webhook_sync.config import SECRET_NAMES

pytestmark = pytest.mark.live

ENV_FILE = Path(os.environ.get("WSYNC_ENV_FILE") or Path(__file__).parents[2] / ".env")
SHEET = "SHEET_ID"
"""The throwaway sheet's id in `.env`."""
SCENES = (("reset",), ("outage", "--orders", "2"), ("verify",))


def test_reset_outage_and_verify_pay_real_test_orders_and_end_on_0_differences(
    tmp_path: Path,
) -> None:
    found = {name: value or "" for name, value in dotenv_values(ENV_FILE).items()}
    secrets = {name: found.get(name, "").strip() for name in SECRET_NAMES}
    sheet = found.get(SHEET, "").strip()
    if not all(secrets.values()) or not sheet:
        pytest.fail(f"{ENV_FILE} must set {', '.join((*SECRET_NAMES, SHEET))}")
    assert secrets["RAZORPAY_KEY_ID"].startswith("rzp_test_"), "test-mode keys only"
    config_path = tmp_path / "webhook-sync.toml"
    config_path.write_text(
        f'[service]\npublic_listen = "127.0.0.1:{free_port()}"\n'
        f'admin_listen = "127.0.0.1:{free_port()}"\ndata_dir = "data"\n\n'
        f'[sources.razorpay]\nmode = "test"\naccount = "{RECORDED_ACCOUNT}"\n\n'
        f'[sinks.sheets]\nspreadsheet_id = "{sheet}"\n\n'
        f'[demo]\nspreadsheet_ids = ["{sheet}"]\n',
        encoding="utf-8",
    )
    environ = {
        k: v for k, v in os.environ.items() if not k.startswith(("RAZORPAY_", "GOOGLE_"))
    } | secrets
    hidden = {
        f"{name} (part {n})": part.strip()
        for name, value in (*secrets.items(), (SHEET, sheet))
        for n, part in enumerate(value.split(","))
        if part.strip()
    }

    printed: list[str] = []
    for scene in SCENES:
        result = subprocess.run(
            [wsync_path(), "demo", *scene, "--config", str(config_path)],
            env=environ,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30 * 60,
            check=False,
        )
        output = result.stdout + result.stderr
        shown = [name for name, value in hidden.items() if value in output]
        for name, value in hidden.items():
            output = output.replace(value, f"<{name}>")
        print(f"\n$ wsync demo {' '.join(scene)}\n{output}", flush=True)
        assert shown == [], f"wsync demo {scene[0]} printed {', '.join(shown)}"
        assert result.returncode == 0, output
        printed.append(output)

    assert "0 differences" in printed[-1]
    assert "Via = sweep" in printed[1]
