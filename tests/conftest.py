"""Fixtures shared by every test.

Each test that touches the database gets a fresh data directory and real SQLite;
time is moved with the `time_machine` fixture, never slept. Add shared helpers below,
one fixture per block, so parallel tickets append rather than edit the same lines.
"""

from pathlib import Path

import pytest


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A fresh, empty data directory for this test."""
    path = tmp_path / "data"
    path.mkdir()
    return path
