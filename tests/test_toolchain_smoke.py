"""The test toolchain works: async tests run, time is moved without sleeping, data dirs are fresh."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import time_machine


async def test_async_tests_run_and_time_moves_without_sleeping(
    time_machine: time_machine.TimeMachineFixture,
) -> None:
    time_machine.move_to(datetime(2026, 10, 2, 9, 0, tzinfo=UTC), tick=False)
    await asyncio.sleep(0)

    time_machine.shift(60)

    assert datetime.now(UTC) == datetime(2026, 10, 2, 9, 1, tzinfo=UTC)


def test_each_test_gets_an_empty_data_dir(data_dir: Path) -> None:
    assert data_dir.is_dir()
    assert list(data_dir.iterdir()) == []
