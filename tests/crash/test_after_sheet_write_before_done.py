"""Crash point `after_sheet_write_before_done`: the server dies after the flusher wrote the sheet
and checked it, and before its jobs were completed (testing.md, "Crash points"; user story 18).

The mirror job was running when the server died, so the new server reclaims it and flushes again;
every write is by key, so that flush finds the row the first one wrote and updates it in place
instead of appending a second. Seam: the real `wsync serve`, killed with `os._exit` and started
again on the same database.
"""

import json

from crash.harness import CrashRun, Server
from support import ReadRows

from webhook_sync.sinks.sheets import PAYMENTS

CAPTURE = "Tj9sMcl7hE9PaT"  # payment.captured of pay_Tj9rKBOXWcsMeE, recorded byte-exact
CAPTURED = "pay_Tj9rKBOXWcsMeE"


def flushes(server: Server) -> list[tuple[int, int]]:
    """Each flush of mirror jobs the server logged: (rows updated, rows appended)."""
    entries = [json.loads(line) for line in server.log.read_text(encoding="utf-8").splitlines()]
    return [
        (entry["rows_updated"], entry["rows_appended"])
        for entry in entries
        if entry["message"] == "sheet flush" and entry["jobs"]
    ]


async def test_a_crash_after_the_sheet_is_written_never_writes_a_second_row(
    crash: CrashRun, read_rows: ReadRows
) -> None:
    razorpay = crash.razorpay

    async with crash.server(crash_at="after_sheet_write_before_done") as first:
        [answered] = await razorpay.deliver(first.client, razorpay.delivery(CAPTURE))
        await first.crashed()
    _, *written = crash.sheets.tab(PAYMENTS).values()
    async with crash.server() as second:
        await crash.settle()

        assert answered.status_code == 202
        assert [row[0] for row in written] == [CAPTURED]
        assert flushes(first) == [(0, 1)]
        assert flushes(second) == [(1, 0)]
        assert await read_rows("SELECT stage, status FROM jobs ORDER BY id") == [
            {"stage": "ledger", "status": "done"},
            {"stage": "mirror", "status": "done"},
        ]
        _, *rows = crash.sheets.tab(PAYMENTS).values()
        assert [row[0] for row in rows] == [CAPTURED]
        await crash.check()
