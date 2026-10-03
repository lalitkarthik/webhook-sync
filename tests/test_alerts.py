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
)
from time_machine import TimeMachineFixture

from webhook_sync.alerts import Alerter
from webhook_sync.db.writer import Writer

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
