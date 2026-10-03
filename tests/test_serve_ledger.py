"""`wsync serve` runs the ledger stage (seam 3: the real subprocess).

Its fetches point at an unreachable API address (`write_serve_config`), so a ledger job fails to
connect and retries: proof the stage is registered, without ever calling Razorpay.
"""

import json
import signal
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import httpx
import pytest
from processes import (
    fixture_environment,
    free_port,
    wait_until_healthy,
    write_serve_config,
    wsync_path,
)
from support import razorpay_sample, sign_razorpay

pytestmark = pytest.mark.unrecorded


def ledger_job(database: Path) -> dict[str, Any] | None:
    """The one ledger job, once its first attempt has finished."""
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT jobs.*, attempts.outcome FROM jobs JOIN attempts ON attempts.job_id = jobs.id"
        ).fetchall()
    return dict(rows[0]) if rows else None


@pytest.mark.posix
def test_serve_runs_the_ledger_stage_and_a_fetch_that_cannot_connect_retries(
    tmp_path: Path,
) -> None:
    port = free_port()
    config_path = write_serve_config(tmp_path / "conf", port)
    database = tmp_path / "conf" / "data" / "webhook-sync.db"
    log_path = tmp_path / "stderr.log"
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_ledger01")

    with log_path.open("wb") as log_file:
        process = subprocess.Popen(
            [wsync_path(), "serve", "--config", str(config_path)],
            env=fixture_environment(),
            stdout=log_file,
            stderr=log_file,
        )
        try:
            base_url = f"http://127.0.0.1:{port}"
            wait_until_healthy(base_url, process)
            delivered = httpx.post(f"{base_url}/webhooks/razorpay", content=body, headers=headers)
            deadline = time.monotonic() + 15
            job = ledger_job(database)
            while job is None and time.monotonic() < deadline:
                time.sleep(0.05)
                job = ledger_job(database)
        finally:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=10)

    assert delivered.status_code == 202
    assert job is not None, "the ledger job never ran"
    assert (job["stage"], job["status"], job["attempts"]) == ("ledger", "waiting", 1)
    assert (job["outcome"], job["last_error_class"]) == ("retry", "retry")
    assert job["run_after"] > job["created_at"]  # backed off, not retried at once
    assert f":{process.pid}@" in job["claimed_by"]
    records = [json.loads(line) for line in log_path.read_text("utf-8").splitlines() if line]
    fetches = [r for r in records if r["message"] == "payment fetch"]
    assert fetches[0]["job_id"] == job["id"]
    assert fetches[0]["payment_key"] == "pay_DESlfW9H8K9uqM"
    assert (fetches[0]["outcome"], fetches[0]["error_class"]) == ("failed", "retry")
