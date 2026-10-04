"""A person at the spreadsheet, between any two calls, and the buyer's own tab and cells (Q168).

Each of a person's actions (sort, edit a cell, delete a row, delete a tab, rename a tab) is a
request FakeSheets applies as the person's edit: now, with `apply`, or straight after a call is
answered, with `after_next`. Each test makes one call, lets the person act after it, and shows
the next call sees what the person did, as Google would. Seam: FakeSheets through respx.
"""

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx
from fakes.sheets import (
    BUYERS_TAB,
    FakeSheets,
    Json,
    delete_row,
    delete_tab,
    edit_cell,
    rename_tab,
    sort_rows,
)

SID = "SHEET_ID"
BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{SID}"
TAB = 7


@pytest.fixture
def fake() -> FakeSheets:
    """A spreadsheet with a tab of three keyed rows below a header."""
    fake = FakeSheets(SID)
    fake.apply(
        [
            {"addSheet": {"properties": {"title": "Payments", "sheetId": TAB}}},
            {
                "updateCells": {
                    "start": {"sheetId": TAB, "rowIndex": 0, "columnIndex": 0},
                    "rows": [_row("Payment ID", "Amount")]
                    + [_row(f"pay_{n}", f"{n}00") for n in ("b", "c", "a")],
                    "fields": "userEnteredValue",
                }
            },
        ]
    )
    return fake


@pytest.fixture
async def client(fake: FakeSheets) -> AsyncIterator[httpx.AsyncClient]:
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        headers = {"authorization": f"Bearer {fake.issue_token()}"}
        async with httpx.AsyncClient(headers=headers) as client:
            yield client


def _row(*texts: str) -> Json:
    return {"values": [{"userEnteredValue": {"stringValue": text}} for text in texts]}


async def titles(client: httpx.AsyncClient) -> list[str]:
    answer = await client.get(BASE, params={"fields": "sheets.properties.title"})
    assert answer.status_code == 200
    return [sheet["properties"]["title"] for sheet in answer.json()["sheets"]]


async def rows(client: httpx.AsyncClient) -> list[list[Any]]:
    answer = await client.post(
        f"{BASE}:getByDataFilter",
        params={"fields": "sheets.data.rowData.values.userEnteredValue"},
        json={"dataFilters": [{"gridRange": {"sheetId": TAB, "endColumnIndex": 2}}]},
    )
    assert answer.status_code == 200
    [data] = answer.json()["sheets"][0]["data"]
    return [
        [next(iter(cell.get("userEnteredValue", {}).values()), None) for cell in row["values"]]
        for row in data["rowData"]
    ]


async def test_a_person_sorts_the_rows_between_two_calls(
    fake: FakeSheets, client: httpx.AsyncClient
) -> None:
    fake.after_next("getByDataFilter", [sort_rows(TAB)])

    before = await rows(client)
    after = await rows(client)

    assert [row[0] for row in before[1:]] == ["pay_b", "pay_c", "pay_a"]
    assert after == [["Payment ID", "Amount"], ["pay_a", "a00"], ["pay_b", "b00"], ["pay_c", "c00"]]


async def test_a_person_sorts_descending(fake: FakeSheets, client: httpx.AsyncClient) -> None:
    fake.apply([sort_rows(TAB, order="DESCENDING")])

    assert [row[0] for row in (await rows(client))[1:]] == ["pay_c", "pay_b", "pay_a"]


async def test_a_person_edits_a_cell_between_two_calls(
    fake: FakeSheets, client: httpx.AsyncClient
) -> None:
    fake.after_next("getByDataFilter", [edit_cell(TAB, 2, 1, "my note")])

    await rows(client)

    assert (await rows(client))[2] == ["pay_c", "my note"]


async def test_a_person_deletes_a_row_between_two_calls(
    fake: FakeSheets, client: httpx.AsyncClient
) -> None:
    fake.after_next("getByDataFilter", [delete_row(TAB, 1)])
    rows_before = fake.tab(TAB).row_count

    await rows(client)

    assert [row[0] for row in (await rows(client))[1:]] == ["pay_c", "pay_a"]
    assert fake.tab(TAB).row_count == rows_before - 1


async def test_a_person_deletes_a_tab_between_two_calls(
    fake: FakeSheets, client: httpx.AsyncClient
) -> None:
    fake.after_next("get", [delete_tab(TAB)])

    assert await titles(client) == ["Sheet1", "Payments"]
    assert await titles(client) == ["Sheet1"]


async def test_a_person_renames_a_tab_between_two_calls(
    fake: FakeSheets, client: httpx.AsyncClient
) -> None:
    fake.after_next("get", [rename_tab(TAB, "Payments (old)")])

    await titles(client)

    assert await titles(client) == ["Sheet1", "Payments (old)"]
    assert [row[0] for row in (await rows(client))[1:]] == ["pay_b", "pay_c", "pay_a"]


async def test_a_person_acts_after_a_write_too(fake: FakeSheets, client: httpx.AsyncClient) -> None:
    fake.after_next("batchUpdate", [edit_cell(TAB, 1, 1, "by hand")])

    written = await client.post(
        f"{BASE}:batchUpdate", json={"requests": [edit_cell(TAB, 3, 1, "by the service")]}
    )

    assert written.status_code == 200
    assert [row[1] for row in (await rows(client))[1:]] == ["by hand", "c00", "by the service"]


def test_the_buyers_own_tab_and_cells_sit_beside_ours() -> None:
    fake = FakeSheets(SID)

    fake.add_buyers_own()

    assert [tab.title for tab in fake.tabs] == ["Sheet1", BUYERS_TAB]
    assert fake.tab("Sheet1").values()[:2] == [["Customer", "Plan"], ["Asha", "Yearly"]]
    budget = fake.tab(BUYERS_TAB)
    assert budget.values()[0] == ["Month", "Target"]
    assert budget.value(1, 1) == 125000.0
    assert budget.cell(1, 1)["note"]
