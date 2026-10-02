"""`wsync serve` as a real subprocess (seam 3): it starts, stores a delivery, logs JSON lines."""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from support import (
    TEST_ACCOUNT,
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_WEBHOOK_SECRET,
    razorpay_sample,
    sign_razorpay,
)

pytestmark = pytest.mark.unrecorded


@dataclass
class Server:
    base_url: str
    config_dir: Path
    log_path: Path


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def fixture_environment() -> dict[str, str]:
    """This process's environment with the fixture secrets in place of any real ones."""
    environ = {k: v for k, v in os.environ.items() if not k.startswith("RAZORPAY_")}
    environ |= {
        "RAZORPAY_KEY_ID": TEST_KEY_ID,
        "RAZORPAY_KEY_SECRET": TEST_KEY_SECRET,
        "RAZORPAY_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET,
    }
    return environ


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    """`wsync serve` started from an unrelated directory, its config in `conf/`."""
    port = free_port()
    config_dir = tmp_path / "conf"
    config_dir.mkdir()
    (config_dir / "webhook-sync.toml").write_text(
        f'[service]\npublic_listen = "127.0.0.1:{port}"\ndata_dir = "data"\n\n'
        f'[sources.razorpay]\nmode = "test"\naccount = "{TEST_ACCOUNT}"\n'
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    wsync = shutil.which("wsync", path=Path(sys.executable).parent)
    assert wsync is not None, "the wsync console script is not installed"
    log_path = tmp_path / "stderr.log"
    with log_path.open("wb") as log_file:
        process = subprocess.Popen(
            [wsync, "serve", "--config", str(config_dir / "webhook-sync.toml")],
            cwd=elsewhere,
            env=fixture_environment(),
            stdout=log_file,
            stderr=log_file,
        )
        base_url = f"http://127.0.0.1:{port}"
        try:
            wait_until_healthy(base_url, process)
            yield Server(base_url, config_dir, log_path)
        finally:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def wait_until_healthy(base_url: str, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        assert process.poll() is None, "wsync serve exited before it was healthy"
        try:
            if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.05)
    pytest.fail("wsync serve did not answer /health in time")


def test_serve_answers_health_and_stores_a_signed_delivery_under_the_config_dir(
    server: Server,
) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_serve01")

    health = httpx.get(f"{server.base_url}/health")
    delivery = httpx.post(f"{server.base_url}/webhooks/razorpay", content=body, headers=headers)

    assert health.status_code == 200
    assert delivery.status_code == 202
    assert (server.config_dir / "data" / "webhook-sync.db").is_file()


def test_serve_logs_json_lines_with_the_event_id_and_no_secret_or_body(server: Server) -> None:
    body, headers = sign_razorpay(razorpay_sample("payment.captured"), event_id="evt_serve02")
    httpx.post(f"{server.base_url}/webhooks/razorpay", content=body, headers=headers)
    bad_body, bad_headers = sign_razorpay(razorpay_sample("order.paid"), secret="wrong-secret")
    httpx.post(f"{server.base_url}/webhooks/razorpay", content=bad_body, headers=bad_headers)

    lines = [line for line in server.log_path.read_text("utf-8").splitlines() if line.strip()]

    records = [json.loads(line) for line in lines]
    assert any(r.get("event_id") == "evt_serve02" for r in records)
    assert any(r.get("reason") == "bad_signature" for r in records)
    log_text = "\n".join(lines)
    forbidden = [
        TEST_KEY_ID,
        TEST_KEY_SECRET,
        TEST_WEBHOOK_SECRET,
        headers["x-razorpay-signature"],
        bad_headers["x-razorpay-signature"],
        "<email>",
        "<phone>",
        "bank_transaction_id",
    ]
    assert [value for value in forbidden if value in log_text] == []
