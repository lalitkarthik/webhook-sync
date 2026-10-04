"""Helpers for driving `wsync` as a real subprocess (seam 3).

Every subprocess gets an explicit environment: this process's, with the fixture secrets in place
of any real `RAZORPAY_*` values.
"""

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from support import TEST_ACCOUNT, TEST_SPREADSHEET_ID, UNREACHABLE_API, fixture_secrets


def wsync_path() -> str:
    wsync = shutil.which("wsync", path=Path(sys.executable).parent)
    assert wsync is not None, "the wsync console script is not installed"
    return wsync


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def fixture_environment() -> dict[str, str]:
    """This process's environment with the fixture secrets in place of any real ones."""
    environ = {k: v for k, v in os.environ.items() if not k.startswith(("RAZORPAY_", "GOOGLE_"))}
    return environ | fixture_secrets()


def write_serve_config(
    config_dir: Path,
    port: int,
    admin_port: int | None = None,
    *,
    account: str = TEST_ACCOUNT,
    api_url: str = UNREACHABLE_API,
    extra: str = "",
) -> Path:
    """A valid test-mode `webhook-sync.toml` in `config_dir`, listening on `port`, its admin
    listener on `admin_port` (a free port if not given), with `extra` TOML at the end.

    Its Razorpay API address is unreachable unless `api_url` names a fake: the ledger worker's
    fetches fail to connect and retry, and never reach Razorpay."""
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "webhook-sync.toml"
    path.write_text(
        f'[service]\npublic_listen = "127.0.0.1:{port}"\ndata_dir = "data"\n'
        f'admin_listen = "127.0.0.1:{admin_port or free_port()}"\n'
        f'timezone = "Asia/Kolkata"\n\n'
        f'[sources.razorpay]\nmode = "test"\naccount = "{account}"\n'
        f'api_url = "{api_url}"\n\n'
        f'[sinks.sheets]\nspreadsheet_id = "{TEST_SPREADSHEET_ID}"\n{extra}',
        encoding="utf-8",
    )
    return path


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


def stop(process: subprocess.Popen[Any]) -> None:
    """Stop the server and every process under it: on Windows `wsync.exe` is a launcher with
    the server as its grandchild, and `terminate` would only end the launcher."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False
        )
    else:
        process.terminate()
    process.wait(timeout=30)
