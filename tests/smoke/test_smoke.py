"""Smoke: a Razorpay delivery to the real `wsync serve` becomes one ledger row.

The installed `wsync serve` runs as a subprocess on fixture secrets. Its Razorpay API is
`tests/fakes/upstreams.py` on 127.0.0.1, answering with the recorded fetch, and its service
account's token endpoint is the same fake, so nothing leaves this machine. The input is P0's
recorded `payment.captured` delivery (`tests/recordings/razorpay/Tj9sMcl7hE9PaT-1.json`),
re-signed with the fixture secret; the ledger row must equal `expected/`'s file.
"""

import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakes.upstreams import start_upstreams, token_uri
from processes import (
    fixture_environment,
    free_port,
    wait_until_healthy,
    write_serve_config,
    wsync_path,
)
from support import RECORDED_ACCOUNT, fixture_service_account_b64, recorded_delivery, sign_razorpay

pytestmark = pytest.mark.smoke

EVENT_ID = "Tj9sMcl7hE9PaT"
PAYMENT = "pay_Tj9rKBOXWcsMeE"
EXPECTED = Path(__file__).parent / "expected" / f"ledger-row-{PAYMENT}.json"
VOLATILE = ("row_hash", "updated_at")
"""Columns that change from run to run, left out of the comparison."""


def ledger_row(database: Path) -> dict[str, Any] | None:
    if not database.is_file():
        return None
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM payments WHERE payment_id = ?", (PAYMENT,)).fetchall()
    return {k: v for k, v in dict(rows[0]).items() if k not in VOLATILE} if rows else None


def stop(process: subprocess.Popen[bytes]) -> None:
    """Stop the server and every process under it: on Windows `wsync.exe` is a launcher with
    the server as its grandchild, and `terminate` would only end the launcher."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False
        )
    else:
        process.terminate()
    process.wait(timeout=30)


def test_a_recorded_delivery_becomes_the_expected_ledger_row(tmp_path: Path) -> None:
    upstreams = start_upstreams()
    fakes_port = upstreams.server_address[1]
    port = free_port()
    config_path = write_serve_config(
        tmp_path / "conf",
        port,
        account=RECORDED_ACCOUNT,
        api_url=f"http://127.0.0.1:{fakes_port}",
    )
    database = tmp_path / "conf" / "data" / "webhook-sync.db"
    environ = fixture_environment() | {
        "GOOGLE_SERVICE_ACCOUNT_B64": fixture_service_account_b64(token_uri=token_uri(fakes_port))
    }
    body, headers = sign_razorpay(recorded_delivery(EVENT_ID), event_id=EVENT_ID)

    with (tmp_path / "serve.log").open("wb") as log_file:
        process = subprocess.Popen(
            [wsync_path(), "serve", "--config", str(config_path)],
            env=environ,
            stdout=log_file,
            stderr=log_file,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            wait_until_healthy(base_url, process)
            delivered = httpx.post(f"{base_url}/webhooks/razorpay", content=body, headers=headers)
            deadline = time.monotonic() + 20
            row = ledger_row(database)
            while row is None and time.monotonic() < deadline:
                time.sleep(0.1)
                row = ledger_row(database)
        finally:
            stop(process)
            upstreams.shutdown()
            upstreams.server_close()

    assert (delivered.status_code, delivered.json()) == (202, {"result": "new"})
    assert row == json.loads(EXPECTED.read_text(encoding="utf-8"))
