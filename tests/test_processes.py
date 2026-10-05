"""The test helper `stop` ends the whole served tree before it returns (seam 3: the real
subprocess). On Windows `wsync.exe` is a launcher with the server under it, so waiting for the
launcher alone can return while the server still holds the database open."""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import processes
import pytest
from processes import (
    fixture_environment,
    free_port,
    stop,
    wait_until_healthy,
    write_serve_config,
    wsync_path,
)

windows = pytest.mark.skipif(sys.platform != "win32", reason="the launcher tree is Windows-only")


@windows
def test_stop_returns_only_once_every_process_of_the_tree_has_exited(tmp_path: Path) -> None:
    from processes import ProcessTree

    port = free_port()
    config_path = write_serve_config(tmp_path / "conf", port)
    database = tmp_path / "conf" / "data" / "webhook-sync.db"
    with (tmp_path / "serve.log").open("wb") as log_file:
        process = subprocess.Popen(
            [wsync_path(), "serve", "--config", str(config_path)],
            env=fixture_environment(),
            stdout=log_file,
            stderr=log_file,
        )
        try:
            wait_until_healthy(f"http://127.0.0.1:{port}", process)
            with ProcessTree(process.pid) as tree:
                assert len(tree) >= 2, "expected the launcher and the server under it"
                stop(process)
                assert tree.running() == []
        finally:
            if process.poll() is None:
                stop(process)

    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as db:
        assert db.execute("SELECT count(*) FROM payments").fetchone() == (0,)


class RecordingKernel32:
    """The real kernel32, noting each process handle opened and each handle closed."""

    def __init__(self, kernel32: Any) -> None:
        self._kernel32 = kernel32
        self.opened: list[int] = []
        self.closed: list[int] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._kernel32, name)

    def OpenProcess(self, *args: Any) -> int:
        handle = self._kernel32.OpenProcess(*args)
        if handle:
            self.opened.append(handle)
        return handle

    def CloseHandle(self, handle: int) -> int:
        self.closed.append(handle)
        return self._kernel32.CloseHandle(handle)


@windows
def test_a_tree_that_fails_part_way_through_opening_closes_the_handles_it_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kernel32 = RecordingKernel32(processes._kernel32())  # pyright: ignore[reportPrivateUsage]
    start_times = iter([0])

    def started_once(_kernel32: Any, _handle: int) -> int:
        return next(start_times)  # the second process's start time can't be read

    monkeypatch.setattr(processes, "_kernel32", lambda: kernel32)
    monkeypatch.setattr(processes, "_started", started_once)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with pytest.raises(StopIteration):
            processes.ProcessTree(os.getpid())
    finally:
        child.kill()
        child.wait(timeout=30)

    assert len(kernel32.opened) == 2
    assert set(kernel32.opened) <= set(kernel32.closed)
