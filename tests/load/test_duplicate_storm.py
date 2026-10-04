"""Load check: the duplicate storm. A thousand concurrent replays of one delivery do no harm
(testing.md, "Load checks"; edge case B2; user stories 23-25).

One recorded, signed delivery is sent 1,000 times at once, a tenth of them under other
`x-razorpay-event-id`s, which only the signed-body hash ties to the stored event (F12, Q86).
The default 50-a-second cap answers what it can't take 429 before any database work, and the
rest queue in order on the one writer, not on SQLite's lock (Q83): every reply is 2xx or 429
`rate_capped`, none 503. Each delivery the cap refused is sent again under the cap, as
Razorpay's retries would, until it is answered 2xx (Q171); then the storm has left one event,
one ledger row, one sheet row and 999 logged duplicates.

Two p99s are printed (Q172): the server's own handling time of the deliveries it accepted,
held under 2 s only under `-m load` on the laptop, with `synchronous=FULL` as it is (Q160); and
the client's, never asserted. Seam: the real `wsync serve`, with the fakes over HTTP.
"""

from collections import Counter

import pytest
from crash.harness import CrashRun
from load.conftest import Report
from load.harness import RATE_CAPPED, percentile, send_at_once
from support import ReadRows

from webhook_sync.sinks.sheets import PAYMENTS

CAPTURE = "Tj9sMcl7hE9PaT"  # payment.captured of pay_Tj9rKBOXWcsMeE, recorded byte-exact
CAPTURED = "pay_Tj9rKBOXWcsMeE"
STORM = 1_000
REPLAYS = tuple(f"Tj9sStorm{n:05d}" for n in range(10))
REPLAYED = 10
"""Each replayed id is sent this many times."""
P99_LIMIT = 2.0
"""Seconds: the server-side p99 of the storm on the development laptop (Q83, Q172)."""


@pytest.mark.load
async def test_a_storm_of_1000_replays_answers_none_503_and_stores_one_event_and_row(
    load: CrashRun, read_rows: ReadRows, report: Report, timing_asserted: bool
) -> None:
    razorpay = load.razorpay
    capture = razorpay.delivery(CAPTURE)

    deliveries = [capture] * (STORM - len(REPLAYS) * REPLAYED) + [
        capture.under(event_id) for event_id in REPLAYS for _ in range(REPLAYED)
    ]

    async with load.server() as server:
        sends = await send_at_once(razorpay, server.client, deliveries)
        storm = sends.at_once
        statuses = Counter(one.status_code for one in storm)
        await load.settle()
        handled = [
            row["duration_ms"] / 1000
            for row in await read_rows("SELECT duration_ms FROM deliveries")
        ]
        client_p99 = percentile([one.elapsed for one in storm], 99)
        server_p99 = percentile(handled, 99)
        report(
            f"duplicate storm: {len(storm):,} of {STORM:,} answered at once: {statuses[200]}"
            f" x 200, {statuses[202]} x 202, {statuses[429]} x 429, {statuses[503]} x 503;"
            f" the refused sent again {len(sends.again):,} times under the cap"
        )
        report(
            f"duplicate storm: server-side p99 {server_p99:.3f} s (the server's own handling"
            f" time of the {len(handled):,} deliveries it accepted)"
        )
        report(
            f"duplicate storm: client-side p99 {client_p99:.3f} s (from sending to the answer,"
            " connection set-up included; never asserted)"
        )

        assert razorpay.unanswered == []
        assert all(
            one.status_code in (200, 202) or (one.status_code, one.answer) == RATE_CAPPED
            for one in storm
        ), f"the storm's replies: {statuses}"
        assert sorted(one.status_code for one in sends.taken) == [200] * (STORM - 1) + [202]
        assert await read_rows("SELECT event_id, payment_key FROM events") == [
            {"event_id": CAPTURE, "payment_key": CAPTURED}
        ]
        assert await read_rows("SELECT payment_id, row_version FROM payments") == [
            {"payment_id": CAPTURED, "row_version": 1}
        ]
        _, *rows = load.sheets.tab(PAYMENTS).values()
        assert [row[0] for row in rows] == [CAPTURED]
        assert await read_rows(
            "SELECT event_id, result, count(*) AS n FROM deliveries"
            " GROUP BY event_id, result ORDER BY event_id, result"
        ) == [
            {"event_id": CAPTURE, "result": "duplicate", "n": STORM - 1},
            {"event_id": CAPTURE, "result": "new", "n": 1},
        ]
        await load.check()
    if timing_asserted:
        assert server_p99 < P99_LIMIT, (
            f"the storm's server-side p99 is {server_p99:.3f} s, not under {P99_LIMIT} s"
        )
