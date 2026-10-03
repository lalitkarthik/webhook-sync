"""SIGTERM and Ctrl-C to `wsync serve` as a real subprocess (seam 3): stopping loses nothing.

The test holds the database's write lock while the server shuts down, so the server is caught
in its shutdown (waiting to release a lease) long enough to be asked for a new delivery.
"""

import asyncio
import json
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
from support import TEST_ACCOUNT, razorpay_sample, sign_razorpay

from webhook_sync.clock import utc_now
from webhook_sync.db.reader import Reader
from webhook_sync.db.writer import Writer
from webhook_sync.queue import Pauses, claim, enqueue

pytestmark = pytest.mark.unrecorded


@contextmanager
def serving(config_path: Path, port: int, log_path: Path) -> Generator[subprocess.Popen[bytes]]:
    """`wsync serve`, healthy; killed at the end if a test left it running."""
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            [wsync_path(), "serve", "--config", str(config_path)],
            env=fixture_environment(),
            stdout=log_file,
            stderr=log_file,
        )
        try:
            wait_until_healthy(f"http://127.0.0.1:{port}", process)
            yield process
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


async def leave_a_job_running(data_dir: Path) -> None:
    """A job the server holds: enqueued and claimed, as a stage runner would."""
    writer = await Writer.open(data_dir)
    try:
        async with writer.transaction() as tx:
            await enqueue(
                tx,
                stage="ledger",
                sink="ledger",
                source="razorpay",
                account=TEST_ACCOUNT,
                payment_key="pay_A",
                event_id="evt_A",
                retry_profile="production",
                now=utc_now(),
            )
        await claim(
            writer,
            stage="ledger",
            sink="ledger",
            limit=1,
            claimed_by="wsync-serving@2026-10-02T09:00:00.000000Z",
            pauses=Pauses(),
            now=utc_now(),
        )
    finally:
        await writer.close()


def read(data_dir: Path, sql: str) -> list[dict[str, object]]:
    async def fetch() -> list[dict[str, object]]:
        async with Reader.open(data_dir) as reader:
            return await reader.fetch_all(sql)

    return asyncio.run(fetch())


def wait_for_log(log_path: Path, message: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        for line in log_path.read_text("utf-8").splitlines():
            if line.startswith("{") and json.loads(line).get("message", "").startswith(message):
                return
        time.sleep(0.02)
    pytest.fail(f"no log line starting {message!r}")


@pytest.mark.posix
@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"])
def test_a_stop_signal_answers_503_releases_the_lease_writes_the_counts_and_frees_the_lock(
    tmp_path: Path, stop_signal: signal.Signals
) -> None:
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    config_path = write_serve_config(tmp_path / "conf", port)
    data_dir = tmp_path / "conf" / "data"
    log_path = tmp_path / "serve.log"
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_late01")
    bad_body, bad_headers = sign_razorpay(razorpay_sample("payment.captured"), secret="wrong")

    with serving(config_path, port, log_path) as server:
        rejected = httpx.post(
            f"{base_url}/webhooks/razorpay", content=bad_body, headers=bad_headers
        )
        asyncio.run(leave_a_job_running(data_dir))
        with closing(sqlite3.connect(data_dir / "webhook-sync.db", isolation_level=None)) as db:
            db.execute("BEGIN IMMEDIATE")  # the server's release waits for this
            server.send_signal(stop_signal)
            wait_for_log(log_path, "shutting down")
            late = httpx.post(f"{base_url}/webhooks/razorpay", content=body, headers=headers)
            db.execute("ROLLBACK")
        exit_code = server.wait(timeout=30)

    assert rejected.status_code == 400
    assert late.status_code == 503
    assert exit_code == 0
    assert read(data_dir, "SELECT * FROM events") == []
    assert read(data_dir, "SELECT * FROM deliveries") == []
    assert read(data_dir, "SELECT status, attempts FROM jobs") == [
        {"status": "waiting", "attempts": 0}
    ]
    assert read(data_dir, "SELECT outcome FROM attempts") == [{"outcome": "released"}]
    assert read(data_dir, "SELECT reason, count FROM rejection_counts") == [
        {"reason": "bad_signature", "count": 1}
    ]
    started = time.monotonic()
    with serving(config_path, port, log_path) as second:
        assert second.poll() is None
    assert time.monotonic() - started < 10
