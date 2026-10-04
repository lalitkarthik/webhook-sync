"""Razorpay's payments and refunds lists by creation time, on Razorpay's clock (#38).

Seam: the adapter's `list_payments` and `list_refunds` against respx, which answers like Razorpay
from the recorded list answers (`tests/recordings/razorpay/api/list/`): newest first, `from` and
`to` both inclusive, `count` items after `skip`.
"""

import json
from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
import respx
from support import (
    RAZORPAY_RECORDINGS,
    RECORDED_ACCOUNT,
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    ReadRows,
)
from time_machine import TimeMachineFixture

from webhook_sync.alerts import Alerter
from webhook_sync.db.writer import Writer
from webhook_sync.queue import Failure, Pauses
from webhook_sync.sources.razorpay import Listed, RazorpayAdapter, api_client

RAZORPAY_API = "https://api.razorpay.test"
LISTS = RAZORPAY_RECORDINGS / "api" / "list"
DATE = "Sat, 03 Oct 2026 08:00:00 GMT"
HOOK = "https://hooks.example.test/services/T000/B000/fixture"
REFUSED = {"error": {"code": "BAD_REQUEST_ERROR", "description": "Authentication failed"}}


def recorded_list(name: str) -> tuple[list[dict[str, Any]], str]:
    """A recorded full list's items, newest first, and its `Date` header."""
    answer = json.loads((LISTS / f"{name}.json").read_text())
    return answer["body"]["items"], answer["headers"]["date"]


def answers_like_razorpay(
    items: Sequence[dict[str, Any]], date: str, calls: list[dict[str, str]]
) -> Any:
    """A respx side effect answering one list call as Razorpay does, recording its query."""

    def answer(request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params)
        calls.append(query)
        start, end = int(query["from"]), int(query["to"])
        count, skip = int(query.get("count", "10")), int(query.get("skip", "0"))
        inside = [item for item in items if start <= item["created_at"] <= end]
        page = inside[skip : skip + count]
        return httpx.Response(
            200,
            json={"entity": "collection", "count": len(page), "items": page},
            headers={"date": date},
        )

    return answer


@pytest.fixture
def razorpay() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=RAZORPAY_API, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def adapter() -> AsyncIterator[RazorpayAdapter]:
    async with api_client(RAZORPAY_API, TEST_KEY_ID, TEST_KEY_SECRET) as api:
        yield RazorpayAdapter(mode="test", account=RECORDED_ACCOUNT, webhook_secrets=(), api=api)


def ok(result: Listed | Failure) -> Listed:
    assert isinstance(result, Listed), result
    return result


def ids(listed: Listed) -> list[str]:
    return [item["id"] for item in listed.items]


async def test_a_range_returns_the_recorded_payments_newest_first_with_both_bounds_inclusive(
    adapter: RazorpayAdapter, razorpay: respx.MockRouter, writer: Writer
) -> None:
    payments, date = recorded_list("payments-list-count100")
    calls: list[dict[str, str]] = []
    razorpay.get("/v1/payments").mock(side_effect=answers_like_razorpay(payments, date, calls))

    # From the oldest captured payment to the failed one, each exactly at its created_at.
    listed = ok(await adapter.list_payments(1790956611, 1790970020, writer=writer, pauses=Pauses()))

    assert ids(listed) == [
        "pay_Tj9sm13R8h2378",
        "pay_Tj9rKBOXWcsMeE",
        "pay_Tj9gmEfed6kRqO",
        "pay_Tj66x2JuEXjIdB",
        "pay_Tj64grZUMO3PEj",
    ]
    assert listed.items[0] == payments[0]
    assert listed.source_now == datetime(2026, 10, 3, 7, 53, 32, tzinfo=UTC)
    assert calls[0] == {"from": "1790956611", "to": "1790970020", "count": "100"}


async def test_a_range_returns_the_recorded_refunds_newest_first_with_both_bounds_inclusive(
    adapter: RazorpayAdapter, razorpay: respx.MockRouter, writer: Writer
) -> None:
    refunds, date = recorded_list("refunds-list-count100")
    calls: list[dict[str, str]] = []
    razorpay.get("/v1/refunds").mock(side_effect=answers_like_razorpay(refunds, date, calls))

    both = ok(await adapter.list_refunds(1790970043, 1790970053, writer=writer, pauses=Pauses()))
    older = ok(await adapter.list_refunds(1790970000, 1790970043, writer=writer, pauses=Pauses()))

    assert ids(both) == ["rfnd_Tj9tMhKlOulF3Q", "rfnd_Tj9tB2hm5WLvww"]
    assert ids(older) == ["rfnd_Tj9tB2hm5WLvww"]
    assert both.source_now == datetime(2026, 10, 3, 7, 52, 50, tzinfo=UTC)
    assert all("status" not in query for query in calls)  # accepted but ignored (G73)


def payments_in_one_second(count: int, created_at: int) -> list[dict[str, Any]]:
    return [
        {"id": f"pay_same{n:06d}", "entity": "payment", "created_at": created_at}
        for n in range(count)
    ]


async def test_150_payments_created_in_one_second_all_come_back_each_once(
    adapter: RazorpayAdapter, razorpay: respx.MockRouter, writer: Writer
) -> None:
    same_second = payments_in_one_second(150, 1790970000)
    older = [{"id": "pay_older", "entity": "payment", "created_at": 1790969000}]
    calls: list[dict[str, str]] = []
    razorpay.get("/v1/payments").mock(
        side_effect=answers_like_razorpay([*same_second, *older], DATE, calls)
    )

    listed = ok(await adapter.list_payments(1790960000, 1790980000, writer=writer, pauses=Pauses()))

    assert ids(listed) == [item["id"] for item in [*same_second, *older]]
    assert [(call["to"], call.get("skip")) for call in calls] == [
        ("1790980000", None),
        ("1790970001", None),  # to = the oldest created_at seen + 1: nothing new
        ("1790970001", "100"),  # so that two-second window is paged with skip
    ]


@pytest.mark.parametrize(
    ("start", "end", "refused"),
    [
        (1790969938000, 1790970020000, "milliseconds"),
        (1790969938, 1790970020000, "milliseconds"),
        (1790970020, 1790969938, "after its end"),
    ],
)
@pytest.mark.parametrize("path", ["/v1/payments", "/v1/refunds"])
async def test_bounds_in_milliseconds_or_a_start_after_the_end_are_refused_before_any_call(
    adapter: RazorpayAdapter,
    writer: Writer,
    razorpay: respx.MockRouter,
    path: str,
    start: int,
    end: int,
    refused: str,
) -> None:
    route = razorpay.get(path).respond(200, json={"entity": "collection", "items": []})
    list_ = adapter.list_payments if path == "/v1/payments" else adapter.list_refunds

    with pytest.raises(ValueError, match=refused):
        await list_(start, end, writer=writer, pauses=Pauses())

    assert not route.called


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("path", ["/v1/payments", "/v1/refunds"])
async def test_a_refused_list_pauses_the_razorpay_source_in_sinks_and_raises_one_alert(
    writer: Writer,
    read_rows: ReadRows,
    time_machine: TimeMachineFixture,
    path: str,
    status: int,
) -> None:
    time_machine.move_to(datetime(2026, 10, 3, 8, 0, tzinfo=UTC), tick=False)
    alerter = Alerter(writer, HOOK)
    pauses = Pauses(on_pause=alerter.paused)
    with respx.mock(assert_all_called=False) as router:
        listing = router.get(f"{RAZORPAY_API}{path}").respond(status, json=REFUSED)
        hook = router.post(HOOK).respond(200, text="ok")
        async with api_client(RAZORPAY_API, TEST_KEY_ID, TEST_KEY_SECRET) as api:
            adapter = RazorpayAdapter(
                mode="test", account=RECORDED_ACCOUNT, webhook_secrets=(), api=api
            )
            list_ = adapter.list_payments if path == "/v1/payments" else adapter.list_refunds
            first = await list_(1790960000, 1790980000, writer=writer, pauses=pauses)
            again = await list_(1790960000, 1790980000, writer=writer, pauses=pauses)
    await alerter.aclose()

    assert (
        first
        == again
        == Failure(
            f"Razorpay answered HTTP {status}", "pause", pause_target="source", http_status=status
        )
    )
    assert listing.call_count == 2
    assert pauses.sources == {"razorpay"}
    assert await read_rows("SELECT name, kind, state, reason, since FROM sinks") == [
        {
            "name": "razorpay",
            "kind": "source",
            "state": "paused",
            "reason": f"Razorpay answered HTTP {status}",
            "since": "2026-10-03T08:00:00.000000Z",
        }
    ]
    calls = cast(list[respx.models.Call], list(hook.calls))
    [alert] = [json.loads(call.request.content) for call in calls]
    assert alert["text"] == (
        f"webhook-sync paused: source razorpay paused: Razorpay answered HTTP {status}"
    )


async def test_a_list_failing_on_a_later_page_retries_as_the_fetch_does_and_pauses_nothing(
    adapter: RazorpayAdapter, razorpay: respx.MockRouter, writer: Writer, read_rows: ReadRows
) -> None:
    full_page = payments_in_one_second(100, 1790970000)
    razorpay.get("/v1/payments").mock(
        side_effect=[
            httpx.Response(200, json={"items": full_page}, headers={"date": DATE}),
            httpx.Response(503, headers={"retry-after": "30"}),
        ]
    )
    pauses = Pauses()

    failed = await adapter.list_payments(1790960000, 1790980000, writer=writer, pauses=pauses)

    assert failed == Failure(
        "Razorpay answered HTTP 503", "retry", retry_after=timedelta(seconds=30), http_status=503
    )
    assert pauses.sources == frozenset()
    assert await read_rows("SELECT name FROM sinks WHERE state = 'paused'") == []
