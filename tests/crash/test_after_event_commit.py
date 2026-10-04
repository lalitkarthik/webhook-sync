"""Crash point `after_event_commit`: the server dies after it stored an event and before it
answered Razorpay (testing.md, "Crash points"; edge case B5; user story 17).

Razorpay got no answer, so it delivers again; the new server finds the event stored and answers
the retry as a duplicate, and the job stored with the event does the work once. Seam: the real
`wsync serve`, killed with `os._exit` and started again on the same database.
"""

from crash.harness import CrashRun
from support import ReadRows

from webhook_sync.sinks.sheets import PAYMENTS

CAPTURE = "Tj9sMcl7hE9PaT"  # payment.captured of pay_Tj9rKBOXWcsMeE, recorded byte-exact
CAPTURED = "pay_Tj9rKBOXWcsMeE"


async def test_a_crash_after_the_event_is_stored_loses_nothing_and_stores_it_once(
    crash: CrashRun, read_rows: ReadRows
) -> None:
    razorpay = crash.razorpay
    capture = razorpay.delivery(CAPTURE)

    async with crash.server(crash_at="after_event_commit") as first:
        unanswered = await razorpay.deliver(first.client, capture)
        await first.crashed()
    async with crash.server() as second:
        [again] = await razorpay.deliver(second.client, capture)
        await crash.settle()

        assert unanswered == []
        assert razorpay.unanswered == [capture]
        assert (again.status_code, again.answer) == (200, {"result": "duplicate"})
        assert await read_rows("SELECT event_id, payment_key FROM events") == [
            {"event_id": CAPTURE, "payment_key": CAPTURED}
        ]
        assert await read_rows("SELECT payment_id, row_version FROM payments") == [
            {"payment_id": CAPTURED, "row_version": 1}
        ]
        _, *rows = crash.sheets.tab(PAYMENTS).values()
        assert [row[0] for row in rows] == [CAPTURED]
        await crash.check()
