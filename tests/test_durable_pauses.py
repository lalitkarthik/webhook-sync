"""Pauses survive a restart (A3, A13): a pause is written to `sinks` with its reason and time,
loaded into the claim-time cache at start, and only `resume` clears it.

Seam: the ledger stage on P1's stage runner against respx-faked Razorpay, and the app's start.
"""

import asyncio
import random
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import respx
from support import (
    RECORDED_ACCOUNT,
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_WEBHOOK_SECRET,
    ReadRows,
    execute_aside,
)
from time_machine import TimeMachineFixture

from webhook_sync.clock import utc_now
from webhook_sync.config import Config
from webhook_sync.db.writer import Writer
from webhook_sync.ledger import ledger_runner
from webhook_sync.queue import Pauses, StageRunner, enqueue, load_pauses, resume
from webhook_sync.receiver.app import create_app
from webhook_sync.sources.razorpay import RazorpayAdapter, api_client

RAZORPAY_API = "https://api.razorpay.test"
NINE = datetime(2026, 10, 2, 20, 30, tzinfo=UTC)
PAYMENT = "pay_Tj9rKBOXWcsMeE"
REFUSED = {"error": {"code": "BAD_REQUEST_ERROR", "description": "Authentication failed"}}
SINKS = "SELECT name, kind, state, reason, since FROM sinks ORDER BY name"


@pytest.fixture(autouse=True)
def frozen_clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(NINE, tick=False)
    return time_machine


@pytest.fixture
def razorpay() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=RAZORPAY_API, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def adapter() -> AsyncIterator[RazorpayAdapter]:
    async with api_client(RAZORPAY_API, TEST_KEY_ID, TEST_KEY_SECRET) as api:
        yield RazorpayAdapter(
            mode="test",
            account=RECORDED_ACCOUNT,
            webhook_secrets=(TEST_WEBHOOK_SECRET,),
            api=api,
        )


async def never_wakes(seconds: float) -> None:
    await asyncio.Event().wait()


def ledger(writer: Writer, adapter: RazorpayAdapter, pauses: Pauses) -> StageRunner:
    return ledger_runner(
        writer,
        {"razorpay": adapter},
        mirror_sinks=("sheets",),
        claimed_by="wsync-test",
        pauses=pauses,
        retry_profile="production",
        rng=random.Random(7),
        sleep=never_wakes,
    )


async def enqueue_ledger_job(writer: Writer, payment_key: str = PAYMENT) -> None:
    async with writer.transaction() as tx:
        await enqueue(
            tx,
            stage="ledger",
            sink="ledger",
            source="razorpay",
            account=RECORDED_ACCOUNT,
            payment_key=payment_key,
            event_id=None,
            retry_profile="production",
            now=utc_now(),
        )


async def test_a_refused_fetch_is_stored_in_sinks_and_still_in_force_after_a_restart(
    writer: Writer, adapter: RazorpayAdapter, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    fetch = razorpay.get(f"/v1/payments/{PAYMENT}").respond(401, json=REFUSED)
    await enqueue_ledger_job(writer)
    await ledger(writer, adapter, Pauses()).run_once()

    restarted = await load_pauses(writer)
    claimed = await ledger(writer, adapter, restarted).run_once()

    assert await read_rows(SINKS) == [
        {
            "name": "razorpay",
            "kind": "source",
            "state": "paused",
            "reason": "Razorpay answered HTTP 401",
            "since": "2026-10-02T20:30:00.000000Z",
        }
    ]
    assert restarted.sources == {"razorpay"}
    assert claimed == 0
    assert fetch.call_count == 1


async def test_a_second_pause_keeps_the_first_reason_and_time(
    writer: Writer,
    adapter: RazorpayAdapter,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    time_machine: TimeMachineFixture,
) -> None:
    razorpay.get(f"/v1/payments/{PAYMENT}").respond(401, json=REFUSED)
    razorpay.get("/v1/payments/pay_Other").respond(403, json=REFUSED)
    await enqueue_ledger_job(writer)
    await ledger(writer, adapter, Pauses()).run_once()
    time_machine.shift(60)
    await enqueue_ledger_job(writer, "pay_Other")

    await ledger(writer, adapter, Pauses()).run_once()  # a stale cache: a second pause

    [row] = await read_rows(SINKS)
    assert (row["reason"], row["since"]) == (
        "Razorpay answered HTTP 401",
        "2026-10-02T20:30:00.000000Z",
    )


async def test_resume_clears_the_pause_in_sinks_and_the_cache_and_claims_start_again(
    writer: Writer, adapter: RazorpayAdapter, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    fetch = razorpay.get(f"/v1/payments/{PAYMENT}").respond(401, json=REFUSED)
    pauses = Pauses()
    await enqueue_ledger_job(writer)
    await ledger(writer, adapter, pauses).run_once()

    resumed = await resume(writer, "razorpay", pauses=pauses, now=utc_now())
    after_resume = await read_rows(SINKS)
    cache_after_resume = pauses.sources
    claimed = await ledger(writer, adapter, pauses).run_once()

    assert resumed == "resumed"
    assert after_resume == [
        {"name": "razorpay", "kind": "source", "state": "active", "reason": None, "since": NOW}
    ]
    assert cache_after_resume == set()
    assert claimed == 1
    assert fetch.call_count == 2


NOW = "2026-10-02T20:30:00.000000Z"


async def test_resume_says_whether_the_name_was_paused_active_or_unknown(
    writer: Writer, data_dir: Path
) -> None:
    await execute_aside(
        data_dir,
        "INSERT INTO sinks (name, kind, state) VALUES ('sheets', 'sink', 'active')",
    )

    assert await resume(writer, "sheets", pauses=None, now=utc_now()) == "active"
    assert await resume(writer, "nothing", pauses=None, now=utc_now()) == "unknown"


async def test_the_app_loads_stored_pauses_at_start_and_lists_the_configured_names(
    config: Config, data_dir: Path, writer: Writer, read_rows: ReadRows
) -> None:
    await execute_aside(
        data_dir,
        "INSERT INTO sinks (name, kind, state, reason, since)"
        f" VALUES ('sheets', 'sink', 'paused', 'spreadsheet missing', '{NOW}')",
    )
    app = create_app(config, data_dir)

    async with app.router.lifespan_context(app):
        pauses: Pauses = app.state.pauses

    assert pauses.sinks == {"sheets"}
    assert await read_rows("SELECT name, kind, state FROM sinks ORDER BY name") == [
        {"name": "razorpay", "kind": "source", "state": "active"},
        {"name": "sheets", "kind": "sink", "state": "paused"},
    ]
