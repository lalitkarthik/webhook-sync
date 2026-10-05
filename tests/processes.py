"""Helpers for driving `wsync` as a real subprocess (seam 3).

Every subprocess gets an explicit environment: this process's, with the fixture secrets in place
of any real `RAZORPAY_*` values.
"""

import ctypes
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import TracebackType
from typing import Any, Self

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
    """Stop the server and every process under it, and return once they have all exited: on
    Windows `wsync.exe` is a launcher with the server as its grandchild, `terminate` would only
    end the launcher, and a killed server may hold the database open after the launcher exits."""
    if sys.platform != "win32":
        process.terminate()
        process.wait(timeout=30)
        return
    with ProcessTree(process.pid) as tree:
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, check=False
        )
        process.wait(timeout=30)
        running = [f"{tree.names[pid]} ({pid})" for pid in tree.running(within=30)]
    assert not running, f"{', '.join(running)} under wsync still running 30 s after taskkill"


SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_OBJECT_0 = 0
SNAPPROCESS = 0x2


class _ProcessEntry(ctypes.Structure):
    _fields_ = [  # PROCESSENTRY32W
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ProcessID", ctypes.c_uint32),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", ctypes.c_uint32),
        ("cntThreads", ctypes.c_uint32),
        ("th32ParentProcessID", ctypes.c_uint32),
        ("pcPriClassBase", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _kernel32() -> Any:
    assert sys.platform == "win32", "process trees are read through the Windows API"
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
    for walk in (kernel32.Process32FirstW, kernel32.Process32NextW):
        walk.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry)]
    return kernel32


def _snapshot(kernel32: Any) -> dict[int, tuple[int, str]]:
    """Every running process's parent pid and executable name, by pid."""
    snapshot = kernel32.CreateToolhelp32Snapshot(SNAPPROCESS, 0)
    assert snapshot not in (None, ctypes.c_void_p(-1).value), "no process snapshot"
    processes: dict[int, tuple[int, str]] = {}
    try:
        entry = _ProcessEntry(dwSize=ctypes.sizeof(_ProcessEntry))
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            processes[entry.th32ProcessID] = (entry.th32ParentProcessID, entry.szExeFile)
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return processes


def _started(kernel32: Any, handle: int) -> int:
    times = [ctypes.c_uint64() for _ in range(4)]
    assert kernel32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)), "no times"
    return times[0].value


class ProcessTree:
    """Handles on a process and every process under it (Windows), taken while they run, so each
    can be waited for: a process's handle is signalled only once it has fully exited and let go
    of its files, and holding it keeps its pid from being reused.

    A process counts as a child only if it started after its parent: a process outlives its
    parent with the parent's pid, which Windows may reuse for a stranger to our tree."""

    def __init__(self, pid: int) -> None:
        self._kernel32 = _kernel32()
        self._handles: dict[int, int] = {}
        self.names: dict[int, str] = {}
        started: dict[int, int] = {}
        processes = _snapshot(self._kernel32)
        tree = [pid]
        for each in tree:
            handle = self._kernel32.OpenProcess(
                SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, each
            )
            if not handle:
                continue
            try:
                started[each] = _started(self._kernel32, handle)
            except BaseException:
                self._kernel32.CloseHandle(handle)
                self._close()
                raise
            parent = processes.get(each, (0, ""))[0]
            if each != pid and started[each] < started[parent]:
                self._kernel32.CloseHandle(handle)
                continue
            self._handles[each] = handle
            self.names[each] = processes.get(each, (0, "?"))[1]
            tree.extend(
                child
                for child, (its_parent, _) in processes.items()
                if its_parent == each and child not in tree
            )

    def _close(self) -> None:
        for handle in self._handles.values():
            self._kernel32.CloseHandle(handle)
        self._handles.clear()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._close()

    def __len__(self) -> int:
        return len(self._handles)

    def running(self, within: float = 0) -> list[int]:
        """The pids still running after waiting up to `within` seconds for them all to exit."""
        deadline = time.monotonic() + within
        return [
            pid
            for pid, handle in self._handles.items()
            if self._kernel32.WaitForSingleObject(
                handle, max(0, int((deadline - time.monotonic()) * 1000))
            )
            != WAIT_OBJECT_0
        ]
