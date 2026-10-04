"""Settle: every scenario, crash test and load check waits the same way before it checks anything
(testing.md, F22). Settled means no job waiting or running, and no source or sink paused unless
the scenario paused it on purpose.
"""

import pytest
from invariants import settle
from scenarios.harness import Scenario
from support import RECORDED_ACCOUNT, ReadRows

from webhook_sync.clock import utc_now
from webhook_sync.db.writer import Writer
from webhook_sync.queue import enqueue, record_pause

CAPTURE = "Tj9sMcl7hE9PaT"
OPEN = "SELECT count(*) AS n FROM jobs WHERE status IN ('waiting', 'running')"


async def a_job(writer: Writer, stage: str, sink: str, payment_id: str) -> None:
    async with writer.transaction() as tx:
        await enqueue(
            tx,
            stage=stage,
            sink=sink,
            source="razorpay",
            account=RECORDED_ACCOUNT,
            payment_key=payment_id,
            event_id=None,
            retry_profile="production",
            now=utc_now(),
        )


async def test_settle_returns_once_no_job_is_waiting_or_running(
    scenario: Scenario, read_rows: ReadRows
) -> None:
    async with scenario.running() as client:
        await scenario.razorpay.deliver(client, scenario.razorpay.delivery(CAPTURE))
        await settle(scenario.data_dir)

        assert await read_rows(OPEN) == [{"n": 0}]
        assert await read_rows("SELECT count(*) AS n FROM jobs WHERE status = 'done'") == [{"n": 2}]


async def test_settle_fails_after_its_time_limit_naming_the_jobs_still_open(
    scenario: Scenario, writer: Writer
) -> None:
    await a_job(writer, "ledger", "ledger", "pay_Tj9rKBOXWcsMeE")
    await a_job(writer, "mirror", "sheets", "pay_Tj64grZUMO3PEj")

    with pytest.raises(AssertionError) as failed:
        await settle(scenario.data_dir, limit=0.2)

    assert str(failed.value) == (
        "not settled after 0.2 s; jobs still open:"
        " job 1 (ledger, pay_Tj9rKBOXWcsMeE) waiting,"
        " job 2 (mirror sheets, pay_Tj64grZUMO3PEj) waiting"
    )


async def test_settle_fails_naming_a_sink_paused_that_the_scenario_did_not_pause(
    scenario: Scenario, writer: Writer
) -> None:
    async with writer.transaction() as tx:
        await record_pause(tx, "sink", "sheets", "Google refused the key", now=utc_now())

    with pytest.raises(AssertionError) as failed:
        await settle(scenario.data_dir, limit=5)

    assert str(failed.value) == "sink sheets paused, not on purpose: Google refused the key"


async def test_a_pause_on_purpose_holds_its_jobs_and_settle_returns(
    scenario: Scenario, writer: Writer
) -> None:
    async with writer.transaction() as tx:
        await record_pause(tx, "sink", "sheets", "Google refused the key", now=utc_now())
    await a_job(writer, "mirror", "sheets", "pay_Tj64grZUMO3PEj")

    await settle(scenario.data_dir, paused={"sheets"}, limit=0.2)
