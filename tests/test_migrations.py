"""Migrations apply at start, once (seam 1: the app's start, real SQLite)."""

from datetime import UTC, datetime
from pathlib import Path

import time_machine
from support import ReadRows

from webhook_sync.config import Config
from webhook_sync.receiver.app import create_app

P1_TABLES = {"events", "deliveries", "rejection_counts", "jobs", "attempts", "schema_version"}
P2_TABLES = {"payments"}
P3_TABLES = {"sinks", "sheet_state", "alerts"}
P4_TABLES = {"checkpoints", "verify_runs", "verify_findings"}


async def start_and_stop(config: Config, data_dir: Path) -> None:
    app = create_app(config, data_dir)
    async with app.router.lifespan_context(app):
        pass


async def test_the_first_start_creates_every_table_and_records_each_version(
    config: Config,
    data_dir: Path,
    read_rows: ReadRows,
    time_machine: time_machine.TimeMachineFixture,
) -> None:
    time_machine.move_to(datetime(2026, 10, 2, 9, 0, tzinfo=UTC), tick=False)

    await start_and_stop(config, data_dir)

    tables = await read_rows("SELECT name FROM sqlite_master WHERE type = 'table'")
    assert {t["name"] for t in tables} >= P1_TABLES | P2_TABLES | P3_TABLES | P4_TABLES
    assert await read_rows("SELECT version, applied_at FROM schema_version") == [
        {"version": 1, "applied_at": "2026-10-02T09:00:00.000000Z"},
        {"version": 2, "applied_at": "2026-10-02T09:00:00.000000Z"},
        {"version": 3, "applied_at": "2026-10-02T09:00:00.000000Z"},
        {"version": 4, "applied_at": "2026-10-02T09:00:00.000000Z"},
    ]


async def test_a_second_start_applies_nothing(
    config: Config,
    data_dir: Path,
    read_rows: ReadRows,
    time_machine: time_machine.TimeMachineFixture,
) -> None:
    time_machine.move_to(datetime(2026, 10, 2, 9, 0, tzinfo=UTC), tick=False)
    await start_and_stop(config, data_dir)
    before = await read_rows("SELECT * FROM schema_version")
    time_machine.shift(3600)

    await start_and_stop(config, data_dir)

    assert await read_rows("SELECT * FROM schema_version") == before


async def test_p4s_tables_hold_a_checkpoint_per_list_and_verify_runs_with_their_findings(
    config: Config, data_dir: Path, read_rows: ReadRows
) -> None:
    await start_and_stop(config, data_dir)
    columns = {
        table: [c["name"] for c in await read_rows(f"PRAGMA table_info({table})")]
        for table in sorted(P4_TABLES)
    }

    assert columns == {
        "checkpoints": [
            "source",
            "account",
            "list",
            "checkpoint_at",
            "last_sweep_at",
            "last_sweep_stored",
            "last_relist_at",
        ],
        "verify_findings": [
            "id",
            "run_id",
            "kind",
            "source",
            "account",
            "payment_id",
            "detail",
            "repair_job_id",
        ],
        "verify_runs": [
            "id",
            "kind",
            "started_at",
            "finished_at",
            "window_days",
            "local_date",
            "payments_checked",
            "findings_count",
            "repaired_count",
            "ignored_events",
            "outcome",
            "status_line",
        ],
    }
