"""Alerts (spec "Alerts", A8, A17, Q139): one JSON POST with `text` and `content` to a Slack or
Discord webhook, each kind at most once an hour with suppressed repeats counted in the next, ids
and counts only, and a warning log line when no URL is set.

Seam: the alert sender against a respx-faked webhook URL, the stage runner's pause, the dead-jobs
check, and the app's lifespan; time moved with time-machine.
"""

import json
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import httpx
import pytest
import respx
from respx import Route
from support import (
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_SPREADSHEET_ID,
    TEST_WEBHOOK_SECRET,
    ReadRows,
    execute_aside,
)
from time_machine import TimeMachineFixture

from webhook_sync.alerts import Alerter
from webhook_sync.clock import to_db_text, utc_now
from webhook_sync.db.writer import Writer
from webhook_sync.queue import (
    ClaimedJob,
    Done,
    Failure,
    Pauses,
    PauseTarget,
    StageFailed,
    StageRunner,
    enqueue,
    resume,
)

HOOK = "https://hooks.example.test/services/T000/B000/fixture"
NINE = datetime(2026, 10, 2, 20, 30, tzinfo=UTC)
ALERTS = "SELECT kind, last_sent_at, suppressed_count FROM alerts ORDER BY kind"
REDACTED = (TEST_SPREADSHEET_ID, TEST_KEY_ID, TEST_KEY_SECRET, TEST_WEBHOOK_SECRET)


@pytest.fixture(autouse=True)
def frozen_clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(NINE, tick=False)
    return time_machine


@pytest.fixture
def hook() -> Iterator[Route]:
    with respx.mock(assert_all_called=False) as router:
        yield router.post(HOOK).respond(200, text="ok")


@pytest.fixture
async def alerter(writer: Writer) -> AsyncIterator[Alerter]:
    sender = Alerter(writer, HOOK, redact=REDACTED)
    try:
        yield sender
    finally:
        await sender.aclose()


def bodies(route: Route) -> list[dict[str, str]]:
    calls = cast(list[respx.models.Call], list(route.calls))
    return [json.loads(call.request.content) for call in calls]


async def test_an_alert_is_one_post_with_text_and_content(
    alerter: Alerter, hook: Route, read_rows: ReadRows
) -> None:
    outcome = await alerter.send("paused", "sink sheets paused: spreadsheet missing")

    [body] = bodies(hook)
    assert outcome == "sent"
    assert body["text"] == body["content"]
    assert "sink sheets paused: spreadsheet missing" in body["text"]
    assert hook.calls.last.request.headers["content-type"] == "application/json"
    assert await read_rows(ALERTS) == [
        {"kind": "paused", "last_sent_at": "2026-10-02T20:30:00.000000Z", "suppressed_count": 0}
    ]


async def test_a_second_alert_of_a_kind_within_the_hour_is_suppressed_and_counted(
    alerter: Alerter,
    hook: Route,
    read_rows: ReadRows,
    time_machine: TimeMachineFixture,
) -> None:
    await alerter.send("paused", "first")
    time_machine.shift(59 * 60)
    second = await alerter.send("paused", "second")
    third = await alerter.send("paused", "third")
    other_kind = await alerter.send("dead_jobs", "another kind")
    suppressed = await read_rows(ALERTS)
    time_machine.shift(60)
    fourth = await alerter.send("paused", "fourth")

    texts = [body["text"] for body in bodies(hook)]
    assert (second, third, other_kind, fourth) == ("suppressed", "suppressed", "sent", "sent")
    assert suppressed[1]["suppressed_count"] == 2
    assert texts[0].endswith("first")
    assert texts[1].endswith("another kind")
    assert "fourth" in texts[2]
    assert "2 more suppressed" in texts[2]
    assert (await read_rows(ALERTS))[1]["suppressed_count"] == 0


async def test_without_a_url_a_warning_line_is_logged_instead(
    writer: Writer, caplog: pytest.LogCaptureFixture
) -> None:
    sender = Alerter(writer, "", redact=REDACTED)
    with respx.mock(assert_all_called=False) as router, caplog.at_level(logging.INFO):
        outcome = await sender.send("paused", "sink sheets paused")
        again = await sender.send("paused", "sink sheets paused")
    await sender.aclose()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert (outcome, again) == ("logged", "suppressed")
    assert len(warnings) == 1
    assert "sink sheets paused" in warnings[0].getMessage()
    assert not router.calls


async def test_no_alert_body_carries_an_email_a_key_or_the_spreadsheet_id(
    alerter: Alerter, hook: Route
) -> None:
    await alerter.send(
        "paused",
        f"sink sheets paused: {TEST_SPREADSHEET_ID} not shared with buyer.name@example.com"
        f" (key {TEST_KEY_ID}, secret {TEST_KEY_SECRET}, {TEST_WEBHOOK_SECRET})",
    )

    [body] = bodies(hook)
    text = json.dumps(body)
    for leaked in (*REDACTED, "buyer.name@example.com", "@example.com"):
        assert leaked not in text
    assert "sink sheets paused" in text


async def test_a_failed_post_is_logged_and_not_retried_before_the_next_due_time(
    writer: Writer, caplog: pytest.LogCaptureFixture
) -> None:
    sender = Alerter(writer, HOOK, redact=REDACTED)
    with respx.mock() as router:
        route = router.post(HOOK).mock(side_effect=httpx.ConnectTimeout("timed out"))
        first = await sender.send("paused", "first")
        second = await sender.send("paused", "second")
    await sender.aclose()

    assert (first, second) == ("failed", "suppressed")
    assert route.call_count == 1
    assert any("alert not sent" in r.getMessage() for r in caplog.records)


async def dead_job(writer: Writer, data_dir: Path, stage: str, payment_key: str) -> None:
    """A job of `stage` that went dead now."""
    async with writer.transaction() as tx:
        await enqueue(
            tx,
            stage=stage,
            sink="ledger" if stage == "ledger" else "sheets",
            source="razorpay",
            account="acc_fixture",
            payment_key=payment_key,
            event_id=None,
            retry_profile="production",
            now=utc_now(),
        )
    await execute_aside(
        data_dir,
        f"UPDATE jobs SET status = 'dead', updated_at = '{to_db_text(utc_now())}'"
        f" WHERE payment_key = '{payment_key}' AND stage = '{stage}'",
    )


async def test_new_dead_jobs_produce_one_alert_with_counts_by_stage(
    alerter: Alerter, writer: Writer, data_dir: Path, hook: Route
) -> None:
    await dead_job(writer, data_dir, "ledger", "pay_A")
    await dead_job(writer, data_dir, "ledger", "pay_B")
    await dead_job(writer, data_dir, "mirror", "pay_A")

    first = await alerter.check_dead_jobs()
    again = await alerter.check_dead_jobs()

    [body] = bodies(hook)
    assert (first, again) == ("sent", None)
    assert body["text"] == "webhook-sync dead_jobs: 3 new dead jobs (ledger 2, mirror 1)"


async def test_no_dead_jobs_alert_when_nothing_new_died(
    alerter: Alerter, writer: Writer, data_dir: Path, hook: Route, time_machine: TimeMachineFixture
) -> None:
    nothing_dead = await alerter.check_dead_jobs()
    await dead_job(writer, data_dir, "ledger", "pay_A")
    await alerter.check_dead_jobs()
    time_machine.shift(2 * 60 * 60)
    nothing_new = await alerter.check_dead_jobs()
    time_machine.shift(1)
    await dead_job(writer, data_dir, "mirror", "pay_B")
    one_new = await alerter.check_dead_jobs()

    texts = [body["text"] for body in bodies(hook)]
    assert (nothing_dead, nothing_new, one_new) == (None, None, "sent")
    assert texts == [
        "webhook-sync dead_jobs: 1 new dead job (ledger 1)",
        "webhook-sync dead_jobs: 1 new dead job (mirror 1)",
    ]


async def test_jobs_dying_within_the_hour_wait_for_the_next_due_check(
    alerter: Alerter, writer: Writer, data_dir: Path, hook: Route, time_machine: TimeMachineFixture
) -> None:
    await dead_job(writer, data_dir, "ledger", "pay_A")
    await alerter.check_dead_jobs()
    time_machine.shift(10 * 60)
    await dead_job(writer, data_dir, "ledger", "pay_B")
    within_the_hour = await alerter.check_dead_jobs()
    time_machine.shift(50 * 60)
    due = await alerter.check_dead_jobs()

    assert (within_the_hour, due) == (None, "sent")
    assert bodies(hook)[1]["text"] == "webhook-sync dead_jobs: 1 new dead job (ledger 1)"


async def mirror_job(writer: Writer, payment_key: str) -> None:
    async with writer.transaction() as tx:
        await enqueue(
            tx,
            stage="mirror",
            sink="sheets",
            source="razorpay",
            account="acc_fixture",
            payment_key=payment_key,
            event_id=None,
            retry_profile="production",
            now=utc_now(),
        )


def pausing_runner(writer: Writer, pauses: Pauses, target: PauseTarget) -> StageRunner:
    """The mirror stage for `sheets`, whose every attempt pauses `target` with a reason."""

    async def work(job: ClaimedJob) -> Done:
        raise StageFailed(Failure("spreadsheet missing", "pause", pause_target=target))

    return StageRunner(
        writer,
        stage="mirror",
        sink="sheets",
        limit=10,
        work=work,
        claimed_by="wsync-test",
        pauses=pauses,
        retry_profile="production",
    )


async def test_a_pause_sends_one_alert_and_a_second_within_the_hour_is_suppressed_and_counted(
    alerter: Alerter,
    writer: Writer,
    hook: Route,
    read_rows: ReadRows,
    time_machine: TimeMachineFixture,
) -> None:
    pauses = Pauses(on_pause=alerter.paused)
    runner = pausing_runner(writer, pauses, "sink")
    await mirror_job(writer, "pay_A")
    await mirror_job(writer, "pay_B")
    await runner.run_once()  # both jobs fail in one batch: one pause, one alert
    await resume(writer, "sheets", pauses=pauses, now=utc_now())
    time_machine.shift(10 * 60)
    await runner.run_once()  # paused again within the hour

    [body] = bodies(hook)
    assert body["text"] == "webhook-sync paused: sink sheets paused: spreadsheet missing"
    assert await read_rows(ALERTS) == [
        {"kind": "paused", "last_sent_at": "2026-10-02T20:30:00.000000Z", "suppressed_count": 1}
    ]


async def test_a_source_pause_names_the_source(
    alerter: Alerter, writer: Writer, hook: Route
) -> None:
    await mirror_job(writer, "pay_A")

    await pausing_runner(writer, Pauses(on_pause=alerter.paused), "source").run_once()

    [body] = bodies(hook)
    assert body["text"] == "webhook-sync paused: source razorpay paused: spreadsheet missing"
