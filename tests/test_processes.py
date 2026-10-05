"""The test helper `stop` ends the whole served tree before it returns (seam 3: the real
subprocess). On Windows `wsync.exe` is a launcher with the server under it, so waiting for the
launcher alone can return while the server still holds the database open."""

import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

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
