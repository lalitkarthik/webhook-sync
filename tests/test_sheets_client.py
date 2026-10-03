"""The Sheets client: its three calls, its token, and how it classes Google's answers (Q141).

Seam: the client against the fake Sheets service served through respx (A18), and, for each
error recorded live, against that recorded answer. No call reaches Google.
"""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import time_machine
from fakes.sheets import FakeSheets
from support import TEST_SPREADSHEET_ID, fixture_service_account

from webhook_sync.sinks.sheets_client import (
    SHEETS_API,
    ServiceAccountTokens,
    SheetsClient,
    SheetsFailure,
)

RECORDINGS = Path(__file__).parent / "recordings" / "sheets"
NOW = datetime(2026, 10, 3, 4, 50, 52, tzinfo=UTC)
KEYS = (
    "sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)),"
    "data(startRow,rowData(values(effectiveValue))))"
)


@dataclass
class Sheets:
    fake: FakeSheets
    client: SheetsClient
    router: respx.MockRouter


@pytest.fixture
async def sheets(time_machine: time_machine.TimeMachineFixture) -> AsyncIterator[Sheets]:
    time_machine.move_to(NOW, tick=False)
    fake = FakeSheets(TEST_SPREADSHEET_ID)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        async with httpx.AsyncClient() as http:
            tokens = ServiceAccountTokens(fixture_service_account())
            yield Sheets(fake, SheetsClient(http, TEST_SPREADSHEET_ID, tokens), router)


def _ok(answer: dict[str, Any] | SheetsFailure) -> dict[str, Any]:
    assert not isinstance(answer, SheetsFailure), answer
    return answer


def _failed(answer: dict[str, Any] | SheetsFailure) -> SheetsFailure:
    assert isinstance(answer, SheetsFailure), answer
    return answer


def _answer_instead(sheets: Sheets) -> respx.Route:
    """A route that answers every Sheets call in place of the fake (the token stays fake's)."""
    sheets.router.clear()
    route = sheets.router.route(host="sheets.googleapis.com")
    sheets.fake.mount(sheets.router)
    return route


def _text(value: str) -> dict[str, Any]:
    return {"userEnteredValue": {"stringValue": value}}


def _write(sheet_id: int, row: int, *cells: dict[str, Any], column: int = 0) -> dict[str, Any]:
    return {
        "updateCells": {
            "start": {"sheetId": sheet_id, "rowIndex": row, "columnIndex": column},
            "rows": [{"values": list(cells)}],
            "fields": "userEnteredValue,userEnteredFormat.numberFormat",
        }
    }


async def _add_tab(sheets: Sheets, title: str, rows: int = 5, columns: int = 4) -> int:
    answer = _ok(
        await sheets.client.batch_update(
            [
                {
                    "addSheet": {
                        "properties": {
                            "title": title,
                            "gridProperties": {"rowCount": rows, "columnCount": columns},
                        }
                    }
                }
            ]
        )
    )
    return answer["replies"][0]["addSheet"]["properties"]["sheetId"]


# -- the calls ---------------------------------------------------------------------------------


async def test_get_lists_every_tab_with_its_sheet_id_and_grid(sheets: Sheets) -> None:
    answer = _ok(await sheets.client.get(fields="sheets.properties"))

    assert answer == {
        "sheets": [
            {
                "properties": {
                    "sheetId": 0,
                    "title": "Sheet1",
                    "index": 0,
                    "sheetType": "GRID",
                    "gridProperties": {"rowCount": 1000, "columnCount": 26},
                }
            }
        ]
    }


async def test_typed_values_round_trip_and_formula_looking_text_stays_text(
    sheets: Sheets,
) -> None:
    payments = await _add_tab(sheets, "Payments")
    _ok(
        await sheets.client.batch_update(
            [
                _write(
                    payments,
                    1,
                    _text("=1+1"),
                    _text("+919876543210"),
                    _text("007"),
                    {
                        "userEnteredValue": {"numberValue": 500.5},
                        "userEnteredFormat": {
                            "numberFormat": {"type": "NUMBER", "pattern": "0.00"}
                        },
                    },
                )
            ]
        )
    )

    answer = _ok(
        await sheets.client.get_by_data_filter(
            [{"gridRange": {"sheetId": payments, "startRowIndex": 1, "endRowIndex": 2}}],
            fields="sheets(data(rowData(values(effectiveValue,formattedValue))))",
        )
    )

    assert answer["sheets"][0]["data"][0]["rowData"][0]["values"] == [
        {"effectiveValue": {"stringValue": "=1+1"}, "formattedValue": "=1+1"},
        {"effectiveValue": {"stringValue": "+919876543210"}, "formattedValue": "+919876543210"},
        {"effectiveValue": {"stringValue": "007"}, "formattedValue": "007"},
        {"effectiveValue": {"numberValue": 500.5}, "formattedValue": "500.50"},
    ]


async def test_a_renamed_tab_is_followed_by_its_sheet_id(sheets: Sheets) -> None:
    payments = await _add_tab(sheets, "Payments")
    sheets.fake.apply(  # a person renames the tab
        [
            {
                "updateSheetProperties": {
                    "properties": {"sheetId": payments, "title": "My payments"},
                    "fields": "title",
                }
            }
        ]
    )

    _ok(await sheets.client.batch_update([_write(payments, 0, _text("Payment ID"))]))
    answer = _ok(
        await sheets.client.get_by_data_filter(
            [{"gridRange": {"sheetId": payments, "startRowIndex": 0, "endRowIndex": 1}}],
            fields=KEYS,
        )
    )

    sheet = answer["sheets"][0]
    assert sheet["properties"]["title"] == "My payments"
    assert sheet["data"][0]["rowData"] == [
        {"values": [{"effectiveValue": {"stringValue": "Payment ID"}}]}
    ]


async def test_a_failed_batch_applies_nothing(sheets: Sheets) -> None:
    payments = await _add_tab(sheets, "Payments", rows=2)

    failure = _failed(
        await sheets.client.batch_update(
            [_write(payments, 0, _text("Payment ID")), _write(payments, 2, _text("pay_1"))]
        )
    )

    assert failure.error_class == "retry"
    assert sheets.fake.tab(payments).values() == []


# -- the token ---------------------------------------------------------------------------------


async def test_a_token_is_fetched_once_and_reused_until_it_expires(
    sheets: Sheets, time_machine: time_machine.TimeMachineFixture
) -> None:
    _ok(await sheets.client.get(fields="sheets.properties"))
    _ok(await sheets.client.get(fields="sheets.properties"))
    assert sheets.fake.token_requests == 1

    time_machine.move_to(NOW + timedelta(minutes=50), tick=False)
    _ok(await sheets.client.get(fields="sheets.properties"))
    assert sheets.fake.token_requests == 1

    time_machine.move_to(NOW + timedelta(hours=1), tick=False)
    _ok(await sheets.client.get(fields="sheets.properties"))
    assert sheets.fake.token_requests == 2
    assert sheets.fake.calls == ["get"] * 4


async def test_a_token_google_refuses_pauses_the_sink(sheets: Sheets) -> None:
    sheets.fake.refuse_tokens = True

    failure = _failed(await sheets.client.get(fields="sheets.properties"))

    assert (failure.error_class, failure.pause_target) == ("pause", "sink")
    assert failure.error == "Google refused the service-account key"
    assert sheets.fake.calls == []


async def test_a_revoked_token_pauses_and_a_fresh_one_is_fetched_next_time(
    sheets: Sheets,
) -> None:
    _ok(await sheets.client.get(fields="sheets.properties"))
    sheets.fake.revoke_tokens()

    failure = _failed(await sheets.client.get(fields="sheets.properties"))
    assert (failure.error_class, failure.http_status) == ("pause", 401)

    _ok(await sheets.client.get(fields="sheets.properties"))
    assert sheets.fake.token_requests == 2


# -- failures, through the fake ----------------------------------------------------------------


async def test_a_deleted_tab_pauses_tab_missing_on_read_and_write(sheets: Sheets) -> None:
    payments = await _add_tab(sheets, "Payments")
    sheets.fake.apply([{"deleteSheet": {"sheetId": payments}}])

    read = _failed(
        await sheets.client.get_by_data_filter(
            [{"gridRange": {"sheetId": payments, "startRowIndex": 0, "endRowIndex": 1}}],
            fields=KEYS,
        )
    )
    write = _failed(await sheets.client.batch_update([_write(payments, 0, _text("x"))]))

    for failure in (read, write):
        assert (failure.error_class, failure.error, failure.pause_target) == (
            "pause",
            "tab missing",
            "sink",
        )


async def test_one_bad_cell_names_its_request(sheets: Sheets) -> None:
    payments = await _add_tab(sheets, "Payments")

    failure = _failed(
        await sheets.client.batch_update(
            [_write(payments, 1, _text("fine")), _write(payments, 2, _text("x" * 50_001))]
        )
    )

    assert (failure.error_class, failure.request_index) == ("dead", 1)


async def test_growing_past_the_cell_limit_pauses_sheet_full(sheets: Sheets) -> None:
    payments = await _add_tab(sheets, "Payments")
    sheets.fake.cell_limit = sheets.fake.cell_count() + 10

    failure = _failed(
        await sheets.client.batch_update(
            [{"appendDimension": {"sheetId": payments, "dimension": "ROWS", "length": 3}}]
        )
    )

    assert (failure.error_class, failure.error) == ("pause", "sheet full")


async def test_an_unshared_spreadsheet_pauses(sheets: Sheets) -> None:
    sheets.fake.access = "view"
    write = _failed(await sheets.client.batch_update([]))
    sheets.fake.access = "none"
    read = _failed(await sheets.client.get(fields="sheets.properties"))

    for failure in (write, read):
        assert (failure.error_class, failure.http_status) == ("pause", 403)


async def test_a_deleted_spreadsheet_pauses_spreadsheet_missing(sheets: Sheets) -> None:
    sheets.fake.deleted = True

    failure = _failed(await sheets.client.get(fields="sheets.properties"))

    assert (failure.error_class, failure.error) == ("pause", "spreadsheet missing")


async def test_a_429_waits_for_the_end_of_its_quota_window(sheets: Sheets) -> None:
    sheets.fake.exhaust_quota("read", NOW - timedelta(seconds=20))

    failure = _failed(await sheets.client.get(fields="sheets.properties"))

    assert (failure.error_class, failure.retry_after) == ("retry", timedelta(seconds=40))


async def test_server_errors_timeouts_and_lost_connections_retry(sheets: Sheets) -> None:
    sheets.fake.fail_next(503)
    server = _failed(await sheets.client.get(fields="sheets.properties"))
    _answer_instead(sheets).mock(side_effect=httpx.ConnectError("down"))
    connection = _failed(await sheets.client.get(fields="sheets.properties"))
    _answer_instead(sheets).mock(side_effect=httpx.ReadTimeout("slow"))
    timeout = _failed(await sheets.client.batch_update([]))

    assert [f.error_class for f in (server, connection, timeout)] == ["retry"] * 3


# -- every recorded error, as Q141 classes it --------------------------------------------------

_CLASSED = {
    "05": ("retry", None, None),
    "16": ("dead", None, 0),
    "17": ("dead", None, 0),
    "18": ("pause", "sheet full", None),
    "19": ("pause", "sheet full", None),
    "41": ("pause", "tab missing", None),
    "43": ("pause", "tab missing", None),
    "44": ("pause", "tab missing", None),
    "45": ("pause", "tab missing", None),
    "46": ("pause", "spreadsheet missing", None),
    "47": ("pause", "spreadsheet missing", None),
    "49": ("pause", None, None),
    "50": ("pause", None, None),
    "51": ("retry", None, None),
}
"""Recording → (class, the pause reason or None for the HTTP text, `request_index`). The
recordings not here are successes, or calls the client never makes (values API, Drive)."""


def _recorded(number: str) -> dict[str, Any]:
    (path,) = RECORDINGS.glob(f"{number}-*.json")
    return json.loads(path.read_text())


def test_every_recorded_sheets_error_is_classed() -> None:
    errors = {
        path.name[:2]
        for path in RECORDINGS.glob("[0-9][0-9]-*.json")
        if (recording := json.loads(path.read_text())).get("response", {}).get("status", 200) >= 400
        and recording["request"]["url"].startswith(SHEETS_API)
        and "/values" not in recording["request"]["url"]
    }
    assert errors == set(_CLASSED)


@pytest.mark.parametrize("number", sorted(_CLASSED))
async def test_a_recorded_error_is_classed_as_q141_says(
    number: str, sheets: Sheets, time_machine: time_machine.TimeMachineFixture
) -> None:
    recording = _recorded(number)
    ran_at = datetime.fromisoformat(recording["ran_at"])
    time_machine.move_to(ran_at, tick=False)
    response = recording["response"]
    _answer_instead(sheets).respond(response["status"], json=response["body"])
    request = recording["request"]
    if request["method"] == "GET":
        answer = await sheets.client.get(fields="sheets.properties")
    elif request["url"].endswith(":getByDataFilter"):
        answer = await sheets.client.get_by_data_filter(
            request["body"]["dataFilters"], fields=request["params"]["fields"]
        )
    else:
        answer = await sheets.client.batch_update(request["body"]["requests"])

    failure = _failed(answer)
    error_class, reason, index = _CLASSED[number]
    assert failure.error_class == error_class
    assert failure.http_status == response["status"]
    assert failure.request_index == index
    if reason is not None:
        assert failure.error == reason
    else:
        assert failure.error.startswith(f"Sheets answered HTTP {response['status']}: ")
    assert (failure.pause_target == "sink") == (error_class == "pause")
    if number == "51":
        window = response["body"]["error"]["details"][0]["metadata"]["window_start_time"]
        ends = datetime.fromtimestamp(int(window), UTC) + timedelta(seconds=60)
        assert failure.retry_after == ends - ran_at
