"""Crash point `mid_slice`: the server dies in the middle of a sweep slice, after its items are
stored and before its checkpoint moves (testing.md, "Crash points"; sweeper.md; user story 19).

The first two slices list nothing, so their checkpoint moves; the third lists the payments and
the server dies there. The checkpoint still stands at the third slice's start, so the new
server's sweep walks that one slice again, and insert-if-absent stores nothing twice; then it
walks on, skipping nothing. Seam: the real `wsync serve`, killed with `os._exit` and started
again on the same database, its sweeps asked for by hand through the admin listener.
"""

import contextlib
from collections.abc import Sequence
from typing import Any

import httpx
from crash.harness import CrashRun
from fakes.razorpay import recorded_listed_payments
from support import ReadRows

from webhook_sync.sinks.sheets import PAYMENTS

HOUR = 3600
SLICE = 6 * HOUR
OVERLAP = 600
"""Each slice after a list's first is requested from 10 minutes before its start."""
CREATED = {"pay_Tj63Ohj3DwqGru", "pay_Tj64GJlPGe5X07"}
"""Listed, but never attempted: nothing is stored for them."""


def placed(at: int) -> dict[str, dict[str, Any]]:
    """The recorded payments, moved in time together so the first was created at `at`."""
    recorded = recorded_listed_payments()
    shift = at - min(item["created_at"] for item in recorded.values())
    return {
        payment_id: item | {"created_at": item["created_at"] + shift}
        for payment_id, item in recorded.items()
    }


def payment_slices(list_calls: Sequence[tuple[str, dict[str, str]]]) -> list[tuple[int, int]]:
    """Every call that walked a slice of the payments list, as (from, to), one per call; a
    one-second call only reads Razorpay's clock."""
    walked = [
        (int(query["from"]), int(query["to"]))
        for path, query in list_calls
        if path == "/v1/payments"
    ]
    return [(start, end) for start, end in walked if end > start]


async def test_a_crash_in_the_middle_of_a_sweep_slice_skips_nothing_and_rewalks_one_slice(
    crash_with_history: CrashRun, read_rows: ReadRows
) -> None:
    crash = crash_with_history
    razorpay = crash.razorpay
    start = int(razorpay.now.timestamp()) - 24 * HOUR  # the first checkpoint: one day back
    first_slice = (start, start + SLICE)
    second_slice = (start + SLICE - OVERLAP, start + 2 * SLICE)
    third_slice = (start + 2 * SLICE - OVERLAP, start + 3 * SLICE)
    razorpay.payments = placed(start + 2 * SLICE + HOUR)  # all inside the third slice
    swept = sorted(set(razorpay.payments) - CREATED)

    async with crash.server(crash_at="mid_slice") as first:
        # Its own first sweep may reach the crash point before this request is answered.
        with contextlib.suppress(httpx.TransportError):
            await first.sweep()
        await first.crashed()
    first_calls = len(razorpay.list_calls)
    before = payment_slices(razorpay.list_calls)
    stored_before = await read_rows("SELECT payment_key FROM events ORDER BY payment_key")
    async with crash.server() as second:
        report = await second.run_ended(await second.sweep())
        await crash.settle()
        after = payment_slices(razorpay.list_calls[first_calls:])

        assert before == [first_slice, second_slice, third_slice]
        assert [row["payment_key"] for row in stored_before] == swept
        assert report["state"] == "finished"
        assert after.count(third_slice) == 1  # the crashed slice, walked again once
        assert first_slice not in after
        assert second_slice not in after
        assert await read_rows("SELECT origin, payment_key FROM events ORDER BY payment_key") == [
            {"origin": "sweep", "payment_key": payment_id} for payment_id in swept
        ]
        assert [
            row["payment_id"] for row in await read_rows("SELECT payment_id FROM payments")
        ] == swept
        _, *rows = crash.sheets.tab(PAYMENTS).values()
        assert sorted(row[0] for row in rows) == swept
        await crash.check()
