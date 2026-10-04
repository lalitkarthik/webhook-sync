"""Load check: the burst. 200 payments arriving together keep the sheet within Google's quota
(testing.md, "Load checks"; F19; user stories 26-27).

200 payments are delivered at once, each a recorded capture under its own payment and event id,
and each fetch takes the time Razorpay's took when recorded (Q167). The cap refuses what it
can't take and those are sent again under it (Q171). One ledger worker fetches them one at a
time, so the work arrives over minutes, and the flusher mirrors it every 3 s: 200 ledger rows,
200 sheet rows, no flush making more than three Sheets calls (two reads and a write), and no
flush sooner than 3 s after the last unless 200 jobs were waiting. The first flush also makes
the three tabs, two calls more, once per spreadsheet (sheets-sink.md step 1). The time the 200
took and the Sheets call count are printed for the README (Q169). It takes minutes, so it runs
only under `-m load` (Q173). Seam: the real `wsync serve`, with the fakes over HTTP.
"""

import copy
import itertools
import json
import time
from datetime import timedelta

import pytest
from crash.harness import CrashRun
from fakes.razorpay import Delivery, FakeRazorpay, recorded_latency
from load.conftest import Report
from load.harness import flushes, send_at_once
from support import ReadRows

from webhook_sync.sinks.sheets import FLUSH_EVERY, FLUSH_LIMIT, PAYMENTS

CAPTURE = "Tj9sMcl7hE9PaT"  # payment.captured of pay_Tj9rKBOXWcsMeE, recorded byte-exact
CAPTURED = "pay_Tj9rKBOXWcsMeE"
BURST = 200
CALLS_PER_FLUSH = 3
SET_UP = ["get", "batchUpdate"]
"""The calls the first flush makes before its own to create the three tabs."""
LEEWAY = timedelta(milliseconds=50)
"""Two clocks read a flush's log line and FakeSheets' calls: what they may differ by."""
SETTLE_LIMIT = 900.0
"""Real seconds 200 fetches one at a time at Razorpay's recorded pace may take, and more."""


@pytest.mark.load
async def test_a_burst_of_200_payments_makes_200_rows_with_at_most_three_sheets_calls_a_flush(
    only_under_load: None, load: CrashRun, read_rows: ReadRows, report: Report
) -> None:
    razorpay = load.razorpay
    razorpay.latency = recorded_latency()
    deliveries = [new_payment(razorpay, n) for n in range(BURST)]
    paid = sorted(delivery.payment_id or "" for delivery in deliveries)

    async with load.server() as server:
        started = time.monotonic()
        sends = await send_at_once(razorpay, server.client, deliveries)
        await load.settle(limit=SETTLE_LIMIT)
        took = time.monotonic() - started
        found = flushes(server.log, load.sheets)
        report(
            f"burst: {BURST} payments mirrored in {took:.1f} s, each fetch taking"
            f" {razorpay.latency:.3f} s; {len(found)} flushes made {len(load.sheets.calls)}"
            f" Sheets calls, {len(SET_UP)} of them creating the tabs"
        )

        assert razorpay.unanswered == []
        assert sorted(one.status_code for one in sends.taken) == [202] * BURST
        assert [
            row["payment_id"]
            for row in await read_rows("SELECT payment_id FROM payments ORDER BY payment_id")
        ] == paid
        _, *rows = load.sheets.tab(PAYMENTS).values()
        assert sorted(row[0] for row in rows) == paid
        first, *_ = found
        assert first.calls[: len(SET_UP)] == SET_UP
        first.calls = first.calls[len(SET_UP) :]
        assert [f.calls for f in found if len(f.calls) > CALLS_PER_FLUSH] == []
        for before, flush in itertools.pairwise(found):
            if FLUSH_LIMIT in (before.jobs, flush.jobs):
                continue  # a full batch is flushed at once
            assert flush.started - before.ended >= FLUSH_EVERY - LEEWAY, (
                f"a flush at {flush.started} came {flush.started - before.ended} after the last"
            )
        await load.check()


def new_payment(razorpay: FakeRazorpay, n: int) -> Delivery:
    """The recorded capture as the `n`th payment of the burst, under its own payment and
    event id, with Razorpay's fetch of it."""
    payment_id = f"pay_Burst{n:09d}"
    state = copy.deepcopy(razorpay.fetches[CAPTURED]) | {"id": payment_id}
    razorpay.fetches[payment_id] = state
    capture = razorpay.delivery(CAPTURE)
    body = json.loads(capture.body)
    body["payload"]["payment"]["entity"]["id"] = payment_id
    return capture.with_body(body).under(f"TjBurst{n:07d}")
