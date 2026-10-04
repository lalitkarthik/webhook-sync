"""The load checks' harness, checked by itself: which flush a Sheets call is counted in."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes.sheets import FakeSheets
from load.harness import flushes
from support import TEST_SPREADSHEET_ID

from webhook_sync.clock import to_db_text

NOON = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
CALLS = ["getByDataFilter", "batchUpdate", "getByDataFilter"]


def test_a_flush_logged_late_still_counts_its_own_calls(tmp_path: Path) -> None:
    # Each flush took 40 ms and its line was logged 10 ms late, so the start worked out from
    # the line falls after the flush's first call.
    late = timedelta(milliseconds=10)
    log = flush_log(tmp_path, [(NOON + late, 40), (NOON + timedelta(seconds=3) + late, 40)])
    sheets = FakeSheets(TEST_SPREADSHEET_ID)
    for began in (NOON - timedelta(milliseconds=40), NOON + timedelta(seconds=3, milliseconds=-40)):
        for n, name in enumerate(CALLS):
            sheets.calls.append(name)
            sheets.called_at.append(began + timedelta(milliseconds=4 + 15 * n))

    assert [flush.calls for flush in flushes(log, sheets)] == [CALLS, CALLS]


def test_a_call_between_two_flushes_is_counted_in_the_next(tmp_path: Path) -> None:
    log = flush_log(tmp_path, [(NOON, 40), (NOON + timedelta(seconds=3), 40)])
    sheets = FakeSheets(TEST_SPREADSHEET_ID)
    sheets.calls.append("batchUpdate")
    sheets.called_at.append(NOON + timedelta(seconds=1))

    assert [flush.calls for flush in flushes(log, sheets)] == [[], ["batchUpdate"]]


def test_a_call_after_the_last_flush_fails(tmp_path: Path) -> None:
    log = flush_log(tmp_path, [(NOON, 40)])
    sheets = FakeSheets(TEST_SPREADSHEET_ID)
    sheets.calls.append("batchUpdate")
    sheets.called_at.append(NOON + timedelta(seconds=1))

    with pytest.raises(AssertionError, match="came after every logged flush"):
        flushes(log, sheets)


def flush_log(tmp_path: Path, logged: list[tuple[datetime, int]]) -> Path:
    """A server's log holding one "sheet flush" line per (time logged, duration in ms), among
    lines that are not flushes."""
    lines = ["Started server process", json.dumps({"time": to_db_text(NOON), "message": "ready"})]
    lines += [
        json.dumps({"time": to_db_text(at), "message": "sheet flush", "jobs": 1, "duration_ms": ms})
        for at, ms in logged
    ]
    path = tmp_path / "serve.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
