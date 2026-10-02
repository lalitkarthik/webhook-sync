"""When Razorpay says no, throttles or is down (seam: the ledger stage on P1's stage runner).

Every answer ends the way the design says (A5, A12, A17): an unknown payment is flagged "not
found at source", or with no row ends dead naming a likely mix-up; throttling, outages and
timeouts retry, honouring `Retry-After` as a minimum; a revoked key pauses the source; anything
else unexpected goes to dead with its reason; fetches stay under 10 a second. Jobs come from
`enqueue`, Razorpay's API is answered by respx with recorded bodies, time by time-machine.
"""

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
import sqlalchemy as sa
from support import (
    RECORDED_ACCOUNT,
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_WEBHOOK_SECRET,
    ReadRows,
    recorded_fetch,
)
from time_machine import TimeMachineFixture

from webhook_sync.clock import utc_now
from webhook_sync.db.schema import jobs
from webhook_sync.db.writer import Writer
from webhook_sync.ledger import ledger_runner
from webhook_sync.logs import JsonFormatter
from webhook_sync.queue import Pauses, StageRunner, enqueue
from webhook_sync.sources.razorpay import RazorpayAdapter, api_client

RAZORPAY_API = "https://api.razorpay.test"
NINE = datetime(2026, 10, 2, 20, 30, tzinfo=UTC)
PAYMENT = "pay_Tj9rKBOXWcsMeE"
FETCH_PATH = f"/v1/payments/{PAYMENT}"
MIX_UP = "not found at source: possible mode or account mix-up"


@pytest.fixture(autouse=True)
def frozen_clock(time_machine: TimeMachineFixture) -> TimeMachineFixture:
    time_machine.move_to(NINE, tick=False)
    return time_machine


@pytest.fixture
def razorpay() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=RAZORPAY_API, assert_all_called=False) as router:
        yield router


def not_found() -> dict[str, Any]:
    return recorded_fetch("not-found-well-formed")


def captured() -> dict[str, Any]:
    return recorded_fetch(f"payment-captured-{PAYMENT}")


async def never_wakes(seconds: float) -> None:
    """The runner's sleep: no heartbeat, no idle wait; the test drives one pass at a time."""
    await asyncio.Event().wait()


@pytest.fixture
def pauses() -> Pauses:
    return Pauses()


@pytest.fixture
async def adapter() -> AsyncIterator[RazorpayAdapter]:
    async with api_client(RAZORPAY_API, TEST_KEY_ID, TEST_KEY_SECRET) as api:
        yield RazorpayAdapter(
            mode="test",
            account=RECORDED_ACCOUNT,
            webhook_secrets=(TEST_WEBHOOK_SECRET,),
            api=api,
        )


@pytest.fixture
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


async def enqueue_ledger_job(
    writer: Writer, event_id: str | None = None, payment_key: str = PAYMENT
) -> int:
    async with writer.transaction() as tx:
        return await enqueue(
            tx,
            stage="ledger",
            sink="ledger",
            source="razorpay",
            account=RECORDED_ACCOUNT,
            payment_key=payment_key,
            event_id=event_id,
            retry_profile="production",
            now=utc_now(),
        )


LEDGER_JOB = (
    "SELECT status, attempts, run_after, last_error, last_error_class"
    " FROM jobs WHERE stage = 'ledger'"
)
ATTEMPTS = "SELECT number, outcome FROM attempts ORDER BY id"


async def test_an_unknown_payment_with_no_row_ends_dead_naming_a_mix_up(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    razorpay.get(FETCH_PATH).respond(400, json=not_found())
    await enqueue_ledger_job(writer, "evt_unknown")

    await ledger.run_once()

    assert await read_rows("SELECT * FROM payments") == []
    [job] = await read_rows(LEDGER_JOB)
    assert (job["status"], job["last_error"], job["last_error_class"]) == ("dead", MIX_UP, "dead")
    assert await read_rows(ATTEMPTS) == [{"number": 1, "outcome": "dead"}]
    assert await read_rows("SELECT * FROM jobs WHERE stage = 'mirror'") == []


async def flusher_takes_the_mirror_jobs(writer: Writer) -> None:
    """Stand-in for P3's flusher: every mirror job is done, so none is live any more."""
    async with writer.transaction() as tx:
        await tx.execute(sa.update(jobs).where(jobs.c.stage == "mirror").values(status="done"))


async def fetch_in_turn(
    writer: Writer,
    ledger: StageRunner,
    clock: TimeMachineFixture,
    *event_ids: str,
) -> None:
    """One ledger job per event, ten minutes apart, each run and its mirror job flushed."""
    for event_id in event_ids:
        await enqueue_ledger_job(writer, event_id)
        await ledger.run_once()
        await flusher_takes_the_mirror_jobs(writer)
        clock.shift(timedelta(minutes=10))


BUSINESS = (
    "SELECT status, amount_minor, fee_minor, net_minor, customer_email, not_found_at,"
    " row_version, last_event_id, updated_at FROM payments"
)
MIRROR_JOBS = "SELECT caused_by_event_id FROM jobs WHERE stage = 'mirror' ORDER BY id"


async def test_an_unknown_payment_with_a_row_keeps_its_values_and_is_flagged_not_found(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    frozen_clock: TimeMachineFixture,
) -> None:
    razorpay.get(FETCH_PATH).mock(
        side_effect=[httpx.Response(200, json=captured()), httpx.Response(400, json=not_found())]
    )
    await fetch_in_turn(writer, ledger, frozen_clock, "evt_captured")
    await enqueue_ledger_job(writer, "evt_later")

    await ledger.run_once()

    assert await read_rows(BUSINESS) == [
        {
            "status": "succeeded",
            "amount_minor": 50000,
            "fee_minor": 1180,
            "net_minor": 48820,
            "customer_email": "customer@example.com",
            "not_found_at": "2026-10-02T20:40:00.000000Z",
            "row_version": 2,
            "last_event_id": "evt_later",
            "updated_at": "2026-10-02T20:40:00.000000Z",
        }
    ]
    assert await read_rows(
        "SELECT caused_by_event_id, status FROM jobs WHERE stage = 'mirror' ORDER BY id"
    ) == [
        {"caused_by_event_id": "evt_captured", "status": "done"},
        {"caused_by_event_id": "evt_later", "status": "waiting"},
    ]
    assert [job["status"] for job in await read_rows(LEDGER_JOB)] == ["done", "done"]
    assert await read_rows(ATTEMPTS) == [
        {"number": 1, "outcome": "ok"},
        {"number": 1, "outcome": "ok"},
    ]


async def test_a_payment_still_not_found_writes_nothing_more(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    frozen_clock: TimeMachineFixture,
) -> None:
    razorpay.get(FETCH_PATH).mock(
        side_effect=[
            httpx.Response(200, json=captured()),
            httpx.Response(400, json=not_found()),
            httpx.Response(400, json=not_found()),
        ]
    )

    await fetch_in_turn(writer, ledger, frozen_clock, "evt_captured", "evt_lost", "evt_again")

    [row] = await read_rows(BUSINESS)
    assert (row["not_found_at"], row["row_version"], row["last_event_id"]) == (
        "2026-10-02T20:40:00.000000Z",
        2,
        "evt_lost",
    )
    assert await read_rows(MIRROR_JOBS) == [
        {"caused_by_event_id": "evt_captured"},
        {"caused_by_event_id": "evt_lost"},
    ]


async def test_a_later_good_fetch_clears_not_found(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    frozen_clock: TimeMachineFixture,
) -> None:
    razorpay.get(FETCH_PATH).mock(
        side_effect=[
            httpx.Response(200, json=captured()),
            httpx.Response(400, json=not_found()),
            httpx.Response(200, json=captured()),
        ]
    )

    await fetch_in_turn(writer, ledger, frozen_clock, "evt_captured", "evt_lost", "evt_back")

    [row] = await read_rows(BUSINESS)
    assert (row["not_found_at"], row["row_version"], row["last_event_id"]) == (
        None,
        3,
        "evt_back",
    )
    assert await read_rows(MIRROR_JOBS) == [
        {"caused_by_event_id": "evt_captured"},
        {"caused_by_event_id": "evt_lost"},
        {"caused_by_event_id": "evt_back"},
    ]


@pytest.mark.parametrize("status", [401, 403])
async def test_a_refused_key_pauses_the_source_and_refunds_the_attempt(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    pauses: Pauses,
    status: int,
) -> None:
    refused = {"error": {"code": "BAD_REQUEST_ERROR", "description": "Authentication failed"}}
    fetch = razorpay.get(FETCH_PATH).respond(status, json=refused)
    await enqueue_ledger_job(writer, "evt_first")
    await ledger.run_once()
    await enqueue_ledger_job(writer, "evt_other", payment_key="pay_Tj9sm13R8h2378")

    assert await ledger.run_once() == 0

    assert pauses.sources == {"razorpay"}
    assert fetch.call_count == 1
    assert await read_rows(LEDGER_JOB) == [
        {
            "status": "waiting",
            "attempts": 0,
            "run_after": "2026-10-02T20:30:00.000000Z",
            "last_error": f"Razorpay answered HTTP {status}",
            "last_error_class": "pause",
        },
        {
            "status": "waiting",
            "attempts": 0,
            "run_after": "2026-10-02T20:30:00.000000Z",
            "last_error": None,
            "last_error_class": None,
        },
    ]
    assert await read_rows(ATTEMPTS) == [{"number": 1, "outcome": "pause"}]


TOO_MANY = {"error": {"code": "BAD_REQUEST_ERROR", "description": "Too many requests"}}
SERVER_ERROR = {
    "error": {
        "code": "SERVER_ERROR",
        "description": "We are facing some trouble completing your request at the moment.",
    }
}


@pytest.mark.parametrize(
    "retry_after", ["120", "Fri, 02 Oct 2026 20:32:00 GMT"], ids=["seconds", "http-date"]
)
async def test_a_throttled_fetch_retries_no_sooner_than_retry_after(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    retry_after: str,
) -> None:
    razorpay.get(FETCH_PATH).respond(429, json=TOO_MANY, headers={"Retry-After": retry_after})
    await enqueue_ledger_job(writer, "evt_first")

    await ledger.run_once()

    [job] = await read_rows(LEDGER_JOB)
    assert (job["status"], job["attempts"], job["last_error_class"]) == ("waiting", 1, "retry")
    assert job["run_after"] >= "2026-10-02T20:32:00.000000Z"
    assert await read_rows(ATTEMPTS) == [{"number": 1, "outcome": "retry"}]
    assert await read_rows("SELECT * FROM payments") == []


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_throttling_and_server_errors_retry_after_the_backoff(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    status: int,
) -> None:
    razorpay.get(FETCH_PATH).respond(status, json=TOO_MANY if status == 429 else SERVER_ERROR)
    await enqueue_ledger_job(writer, "evt_first")

    await ledger.run_once()

    [job] = await read_rows(LEDGER_JOB)
    assert (job["status"], job["attempts"], job["last_error"], job["last_error_class"]) == (
        "waiting",
        1,
        f"Razorpay answered HTTP {status}",
        "retry",
    )
    # The first production wait is 5 s, times a jitter between 0.5 and 1.5.
    assert "2026-10-02T20:30:02.5" <= job["run_after"] <= "2026-10-02T20:30:07.5"
    assert await read_rows(ATTEMPTS) == [{"number": 1, "outcome": "retry"}]


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (
            httpx.Response(
                400,
                json={
                    "error": {
                        "code": "BAD_REQUEST_ERROR",
                        "description": "expand is/are not required and should not be sent",
                    }
                },
            ),
            "Razorpay answered HTTP 400: expand is/are not required and should not be sent",
        ),
        (
            httpx.Response(404, text="<html>Not Found</html>"),
            "Razorpay answered HTTP 404",
        ),
        (
            httpx.Response(200, text="<html>maintenance</html>"),
            "Razorpay answered HTTP 200 with a body that is not a JSON object",
        ),
        (
            httpx.Response(200, json=[captured()]),
            "Razorpay answered HTTP 200 with a body that is not a JSON object",
        ),
    ],
    ids=["other-400", "404-html", "200-html", "200-json-list"],
)
async def test_an_unexpected_answer_sends_the_job_to_dead_with_its_reason(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    answer: httpx.Response,
    reason: str,
) -> None:
    razorpay.get(FETCH_PATH).mock(return_value=answer)
    await enqueue_ledger_job(writer, "evt_first")

    await ledger.run_once()

    [job] = await read_rows(LEDGER_JOB)
    assert (job["status"], job["last_error"], job["last_error_class"]) == ("dead", reason, "dead")
    assert await read_rows(ATTEMPTS) == [{"number": 1, "outcome": "dead"}]
    assert await read_rows("SELECT * FROM payments") == []


PARTLY_REFUNDED = "pay_Tj9gmEfed6kRqO"


def with_ten_refunds() -> dict[str, Any]:
    """The partly refunded payment with a full expanded page of refunds: the rest are paged."""
    recorded = recorded_fetch(f"payment-captured-partly-refunded-{PARTLY_REFUNDED}")
    template = recorded["refunds"]["items"][0]
    items = [{**template, "id": f"rfnd_Test{n:010d}", "amount": 100} for n in range(10)]
    return {**recorded, "refunds": {"entity": "collection", "count": 10, "items": items}}


@pytest.mark.parametrize(
    ("answer", "status", "error_class"),
    [
        (httpx.Response(429, json=TOO_MANY, headers={"Retry-After": "120"}), "waiting", "retry"),
        (httpx.Response(503, json=SERVER_ERROR), "waiting", "retry"),
        (httpx.Response(401, json={}), "waiting", "pause"),
        (httpx.Response(200, text="not json"), "dead", "dead"),
    ],
    ids=["429", "503", "401", "200-not-json"],
)
async def test_a_refunds_page_is_classified_as_the_main_fetch_is(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    answer: httpx.Response,
    status: str,
    error_class: str,
) -> None:
    razorpay.get(f"/v1/payments/{PARTLY_REFUNDED}").respond(200, json=with_ten_refunds())
    razorpay.get(f"/v1/payments/{PARTLY_REFUNDED}/refunds").mock(return_value=answer)
    await enqueue_ledger_job(writer, "evt_first", payment_key=PARTLY_REFUNDED)

    await ledger.run_once()

    [job] = await read_rows(LEDGER_JOB)
    assert (job["status"], job["last_error_class"]) == (status, error_class)
    if answer.status_code == 429:
        assert job["run_after"] >= "2026-10-02T20:32:00.000000Z"
    assert await read_rows(ATTEMPTS) == [{"number": 1, "outcome": error_class}]
    assert await read_rows("SELECT * FROM payments") == []


async def test_a_hung_fetch_ends_at_the_timeout_as_retry(
    writer: Writer, pauses: Pauses, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    async def hangs(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    razorpay.get(FETCH_PATH).mock(side_effect=hangs)
    await enqueue_ledger_job(writer, "evt_first")
    async with api_client(RAZORPAY_API, TEST_KEY_ID, TEST_KEY_SECRET) as api:
        adapter = RazorpayAdapter(
            mode="test",
            account=RECORDED_ACCOUNT,
            webhook_secrets=(TEST_WEBHOOK_SECRET,),
            api=api,
            fetch_timeout=timedelta(milliseconds=50),  # 10 s in production (A11)
        )
        ledger = ledger_runner(
            writer,
            {"razorpay": adapter},
            mirror_sinks=("sheets",),
            claimed_by="wsync-test",
            pauses=pauses,
            retry_profile="production",
            rng=random.Random(7),
            sleep=never_wakes,
        )

        await asyncio.wait_for(ledger.run_once(), timeout=5)

    [job] = await read_rows(LEDGER_JOB)
    assert (job["status"], job["last_error"], job["last_error_class"]) == (
        "waiting",
        "Razorpay call failed: no answer within the timeout",
        "retry",
    )
    assert await read_rows(ATTEMPTS) == [{"number": 1, "outcome": "retry"}]


async def test_an_eleventh_fetch_in_the_same_instant_waits_for_the_bucket(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    read_rows: ReadRows,
    frozen_clock: TimeMachineFixture,
) -> None:
    fetch = razorpay.get(path__regex=r"^/v1/payments/pay_Test\d+$").respond(200, json=captured())
    for n in range(11):
        await enqueue_ledger_job(writer, f"evt_{n}", payment_key=f"pay_Test{n:02d}")
    for _ in range(10):
        await ledger.run_once()
    assert fetch.call_count == 10

    eleventh = asyncio.create_task(ledger.run_once())
    await asyncio.sleep(0.3)  # real time; the clock stands still, so the bucket stays empty
    assert fetch.call_count == 10
    assert not eleventh.done()

    frozen_clock.shift(timedelta(milliseconds=100))  # one token's worth at 10 a second
    await asyncio.wait_for(eleventh, timeout=5)

    assert fetch.call_count == 11
    assert len(await read_rows("SELECT * FROM payments")) == 11


@pytest.mark.parametrize(
    ("answer", "logged"),
    [
        (
            httpx.Response(200, json=captured()),
            {"outcome": "fetched"},
        ),
        (
            httpx.Response(400, json=not_found()),
            {"outcome": "not_found", "error_class": "dead", "http_status": 400},
        ),
        (
            httpx.Response(429, json=TOO_MANY, headers={"Retry-After": "120"}),
            {"outcome": "failed", "error_class": "retry", "http_status": 429},
        ),
        (
            httpx.Response(401, json={}),
            {"outcome": "failed", "error_class": "pause", "http_status": 401},
        ),
        (
            httpx.Response(200, json={**captured(), "amount": -100}),
            {"outcome": "failed", "error_class": "dead"},
        ),
    ],
    ids=["fetched", "not-found", "throttled", "refused", "bad-amount"],
)
async def test_each_outcome_logs_one_line_with_its_class_and_status_and_no_personal_data(
    writer: Writer,
    ledger: StageRunner,
    razorpay: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
    answer: httpx.Response,
    logged: dict[str, Any],
) -> None:
    caplog.set_level(logging.DEBUG)
    razorpay.get(FETCH_PATH).mock(return_value=answer)
    job_id = await enqueue_ledger_job(writer, "evt_first")

    await ledger.run_once()

    lines = [
        json.loads(JsonFormatter().format(record))
        for record in caplog.records
        if record.getMessage() == "payment fetch"
    ]
    assert [
        {k: v for k, v in line.items() if k not in ("time", "level", "logger", "message")}
        for line in lines
    ] == [{"job_id": job_id, "payment_key": PAYMENT, "source": "razorpay", **logged}]
    ours = [record for record in caplog.records if record.name.startswith("webhook_sync")]
    everything = "\n".join(json.dumps(vars(record), default=str) for record in ours)
    for secret in (TEST_KEY_ID, TEST_KEY_SECRET, "customer@example.com", "BAD_REQUEST_ERROR"):
        assert secret not in everything


async def test_a_bad_amount_is_a_dead_failure_with_its_reason(
    writer: Writer, ledger: StageRunner, razorpay: respx.MockRouter, read_rows: ReadRows
) -> None:
    razorpay.get(FETCH_PATH).respond(200, json={**captured(), "amount": 499.5})
    await enqueue_ledger_job(writer, "evt_first")

    await ledger.run_once()

    [job] = await read_rows(LEDGER_JOB)
    assert (job["status"], job["last_error"], job["last_error_class"]) == (
        "dead",
        "bad amount from Razorpay: 499.5 is not a non-negative integer",
        "dead",
    )
    assert await read_rows(ATTEMPTS) == [{"number": 1, "outcome": "dead"}]
