"""Startup reclaim (A13), through `start`, the startup function `wsync serve` calls.

One server per database (ADR 0007) makes every job still `running` at start a leftover of a
crashed run: it goes back to waiting, its attempt counted, with an `attempts` row `reaped`.
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from support import TEST_ACCOUNT, TEST_SPREADSHEET_ID, fixture_secrets
from time_machine import TimeMachineFixture

from webhook_sync.cli.serve import start
from webhook_sync.clock import utc_now
from webhook_sync.db.migrations import apply_pending
from webhook_sync.db.reader import Reader
from webhook_sync.db.writer import Writer
from webhook_sync.queue import Pauses, claim, enqueue

NINE = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)

TOML = f"""
[service]
data_dir = "data"

[sources.razorpay]
mode = "test"
account = "{TEST_ACCOUNT}"

[sinks.sheets]
spreadsheet_id = "{TEST_SPREADSHEET_ID}"
"""


@pytest.fixture(autouse=True)
def frozen_clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(NINE, tick=False)
    return time_machine


async def leave_jobs_running(data_dir: Path, *payment_keys: str) -> None:
    """A crashed run's leftovers: jobs claimed and never finished, their leases still valid."""
    writer = await Writer.open(data_dir)
    try:
        await apply_pending(writer)
        for key in payment_keys:
            async with writer.transaction() as tx:
                await enqueue(
                    tx,
                    stage="ledger",
                    sink="ledger",
                    source="razorpay",
                    account=TEST_ACCOUNT,
                    payment_key=key,
                    event_id=f"evt_{key}",
                    retry_profile="production",
                    now=utc_now(),
                )
        await claim(
            writer,
            stage="ledger",
            sink="ledger",
            limit=len(payment_keys),
            claimed_by="wsync-crashed@2026-10-02T08:00:00.000000Z",
            pauses=Pauses(),
            now=utc_now(),
        )
    finally:
        await writer.close()


async def read(data_dir: Path, sql: str) -> list[dict[str, object]]:
    async with Reader.open(data_dir) as reader:
        return await reader.fetch_all(sql)


def test_jobs_left_running_are_back_to_waiting_after_a_start_with_the_attempt_counted(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "webhook-sync.toml"
    config_path.write_text(TOML, encoding="utf-8")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    asyncio.run(leave_jobs_running(data_dir, "pay_A", "pay_B"))

    with start(config_path, fixture_secrets()):
        pass

    jobs = asyncio.run(read(data_dir, "SELECT status, attempts, claim_token FROM jobs"))
    assert jobs == [{"status": "waiting", "attempts": 1, "claim_token": None}] * 2
    attempts = asyncio.run(read(data_dir, "SELECT number, outcome FROM attempts"))
    assert attempts == [{"number": 1, "outcome": "reaped"}] * 2
