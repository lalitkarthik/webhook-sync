"""Crash point `mid_slice`: the server dies in the middle of a sweep slice, after its items are
stored and before its checkpoint moves (testing.md, "Crash points"; sweeper.md; user story 19).

The checkpoint still stands at the slice's start, so the new server's sweep walks that one slice
again, and insert-if-absent stores nothing twice; then it walks on, skipping nothing. Seam: the
real `wsync serve`, killed with `os._exit` and started again on the same database, its sweeps
asked for by hand through the admin listener.
"""

from collections.abc import Sequence
from typing import Any

from crash.harness import CrashRun
from fakes.razorpay import recorded_listed_payments
from support import ReadRows

from webhook_sync.sinks.sheets import PAYMENTS

HOUR = 3600
SLICE = 6 * HOUR
CREATED = {"pay_Tj63Ohj3DwqGru", "pay_Tj64GJlPGe5X07"}
"""Listed, but never attempted: nothing is stored for them."""
FIRST_SLICE = [
    "pay_Tj63Ohj3DwqGru",
    "pay_Tj64GJlPGe5X07",
    "pay_Tj64grZUMO3PEj",
    "pay_Tj66x2JuEXjIdB",
]
THIRD_SLICE = ["pay_Tj9gmEfed6kRqO", "pay_Tj9rKBOXWcsMeE", "pay_Tj9sm13R8h2378"]


def placed(at: int, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """The recorded payments `ids`, moved in time together so the first was created at `at`."""
    recorded = recorded_listed_payments()
    shift = at - min(recorded[payment_id]["created_at"] for payment_id in ids)
    return {
        payment_id: recorded[payment_id]
        | {"created_at": recorded[payment_id]["created_at"] + shift}
        for payment_id in ids
    }


def slices(list_calls: Sequence[tuple[str, dict[str, str]]]) -> set[tuple[str, int, int]]:
    """The slices the list calls walked: (list, from, to); a one-second call reads the clock."""
    walked = {(path, int(query["from"]), int(query["to"])) for path, query in list_calls}
    return {(path, start, end) for path, start, end in walked if end > start}


async def test_a_crash_in_the_middle_of_a_sweep_slice_skips_nothing_and_rewalks_one_slice(
    crash_with_history: CrashRun, read_rows: ReadRows
) -> None:
    crash = crash_with_history
    razorpay = crash.razorpay
    start = int(razorpay.now.timestamp()) - 24 * HOUR  # the first checkpoint: one day back
    razorpay.payments = placed(start + HOUR, FIRST_SLICE) | placed(
        start + 2 * SLICE + HOUR, THIRD_SLICE
    )
    swept = sorted(set(razorpay.payments) - CREATED)

    async with crash.server(crash_at="mid_slice") as first:
        await first.sweep()
        await first.crashed()
    first_calls = len(razorpay.list_calls)
    before = slices(razorpay.list_calls)
    stored_before = await read_rows("SELECT payment_key FROM events ORDER BY payment_key")
    async with crash.server() as second:
        report = await second.run_ended(await second.sweep())
        await crash.settle()
        after = slices(razorpay.list_calls[first_calls:])

        assert before == {("/v1/payments", start, start + SLICE)}
        assert [row["payment_key"] for row in stored_before] == [
            "pay_Tj64grZUMO3PEj",
            "pay_Tj66x2JuEXjIdB",
        ]
        assert report["state"] == "finished"
        assert before & after == before  # the crashed slice, and only it, walked again
        assert await read_rows("SELECT origin, payment_key FROM events ORDER BY payment_key") == [
            {"origin": "sweep", "payment_key": payment_id} for payment_id in swept
        ]
        assert [
            row["payment_id"] for row in await read_rows("SELECT payment_id FROM payments")
        ] == swept
        _, *rows = crash.sheets.tab(PAYMENTS).values()
        assert sorted(row[0] for row in rows) == swept
        await crash.check()
