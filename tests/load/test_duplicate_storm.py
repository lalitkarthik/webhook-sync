"""Load check: the duplicate storm. A thousand concurrent replays of one delivery do no harm
(testing.md, "Load checks"; edge case B2; user stories 23-25).

One recorded, signed delivery is sent 1,000 times at once, a tenth of them under other
`x-razorpay-event-id`s, which only the signed-body hash ties to the stored event (F12, Q86).
They queue in order on the one writer, not on SQLite's lock (Q83): every reply is 2xx, none
503, and the storm leaves one event, one ledger row, one sheet row and 999 logged duplicates.
The p99 is printed; it is held under 2 s only under `-m load` on the laptop (Q161), with
`synchronous=FULL` as it is (Q160). Seam: the real `wsync serve`, with the fakes over HTTP.
"""

import asyncio
import math
from collections import Counter

import pytest
from crash.harness import CrashRun
from load.conftest import Report
from support import ReadRows

from webhook_sync.sinks.sheets import PAYMENTS

CAPTURE = "Tj9sMcl7hE9PaT"  # payment.captured of pay_Tj9rKBOXWcsMeE, recorded byte-exact
CAPTURED = "pay_Tj9rKBOXWcsMeE"
STORM = 1_000
REPLAYS = tuple(f"Tj9sStorm{n:05d}" for n in range(10))
REPLAYED = 10
"""Each replayed id is sent this many times."""
P99_LIMIT = 2.0
"""Seconds: the storm's p99 on the development laptop (Q83)."""


@pytest.mark.load
async def test_a_storm_of_1000_replays_answers_every_one_2xx_and_stores_one_event_and_row(
    load: CrashRun, read_rows: ReadRows, report: Report, timing_asserted: bool
) -> None:
    razorpay = load.razorpay
    capture = razorpay.delivery(CAPTURE)

    async with load.server() as server:
        batches = await asyncio.gather(
            razorpay.deliver(server.client, capture, times=STORM - len(REPLAYS) * REPLAYED),
            *(
                razorpay.deliver(server.client, capture.under(event_id), times=REPLAYED)
                for event_id in REPLAYS
            ),
        )
        sent = [one for batch in batches for one in batch]
        statuses = Counter(one.status_code for one in sent)
        p99 = percentile([one.elapsed for one in sent], 99)
        report(
            f"duplicate storm: {len(sent):,} of {STORM:,} answered, {statuses[503]} answered"
            f" 503, p99 {p99:.3f} s, slowest {max(one.elapsed for one in sent):.3f} s"
        )
        await load.settle()

        assert razorpay.unanswered == []
        assert sorted(one.status_code for one in sent) == [200] * (STORM - 1) + [202]
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
        assert p99 < P99_LIMIT, f"the storm's p99 is {p99:.3f} s, not under {P99_LIMIT} s"


def percentile(values: list[float], p: int) -> float:
    """The nearest-rank `p`th percentile."""
    ordered = sorted(values)
    return ordered[math.ceil(p / 100 * len(ordered)) - 1]
