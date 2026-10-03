"""The fake Sheets service answers as Google did (A18).

Every recorded call in `tests/recordings/sheets/` that the fake covers is replayed against it in
the order it was recorded, on one spreadsheet that starts as the live one did, and must get the
recorded status and body back. Calls outside the flusher's surface are listed with the reason
they are not replayed, so a new recording can't be skipped silently.
"""

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import respx
import time_machine
from fakes.sheets import FakeSheets

RECORDINGS = Path(__file__).parent / "recordings" / "sheets"
SAMPLE_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"
"""Google's public sample spreadsheet: viewable by anyone with the link, editable by no one."""

NOT_REPLAYED = {
    "08": "values.get by an A1 title: the flusher reads by sheetId",
    "09": "values.get by an A1 title",
    "10": "values.get by an A1 title",
    "12": "get without a field mask: the flusher always sends one",
    "13": "get with grid data by an A1 range",
    "14": "get with grid data by an A1 range",
    "20": "values.get by an A1 title",
    "23": "developer metadata: not used (sheets-sink.md, Further Notes)",
    "24": "developer metadata",
    "28": "values.get by an A1 title",
    "30": "developer metadata",
    "31": "the Drive API",
    "32": "the Drive API",
    "37": "values.get by an A1 title",
    "38": "developer metadata",
    "39": "values by developer metadata (it changes columns B:C of row 6 on tab B; nothing "
    "replayed later reads them)",
    "40": "values by developer metadata",
    "52": "a summary of a burst, not one call",
    "56": "developer metadata",
}

_PLACEHOLDER = re.compile(r"<'x' \* (\d+)(?: \+ U\+([0-9A-F]+))?>")


def _recordings() -> Iterator[tuple[str, dict[str, Any]]]:
    for path in sorted(RECORDINGS.glob("[0-9][0-9]-*.json")):
        yield path.name[:2], json.loads(path.read_text())


def _expand(value: Any) -> Any:
    """The request as sent: the 50,000-character strings in 15-17 were shortened on record."""
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in cast(dict[str, Any], value).items()}
    if isinstance(value, list):
        return [_expand(v) for v in cast(list[Any], value)]
    if isinstance(value, str) and (match := _PLACEHOLDER.fullmatch(value)):
        tail = chr(int(match.group(2), 16)) if match.group(2) else ""
        return "x" * int(match.group(1)) + tail
    return value


def _live_spreadsheet() -> FakeSheets:
    """The owner's throwaway spreadsheet as the live check found it (recordings 01, 02), with
    the sheetIds Google gave the spike's tabs."""
    recorded = dict(_recordings())
    return FakeSheets(
        "SHEET_ID",
        properties=recorded["02"]["response"]["body"]["properties"],
        sheet_ids=[1600034012, 1903783590, 1727956071],
    )


async def test_every_recorded_call_the_fake_covers_is_answered_as_google_did(
    time_machine: time_machine.TimeMachineFixture,
) -> None:
    sheet = _live_spreadsheet()
    sample = FakeSheets(SAMPLE_ID, properties={"title": "Example Spreadsheet"}, access="view")
    differences: list[str] = []
    replayed = 0
    for number, recording in _recordings():
        if number in NOT_REPLAYED:
            continue
        request, expected = recording["request"], recording["response"]
        time_machine.move_to(datetime.fromisoformat(recording["ran_at"]), tick=False)
        fake = sample if SAMPLE_ID in request["url"] else sheet
        token = "not-a-token" if number == "50" else fake.issue_token()
        if number == "51":
            metadata = expected["body"]["error"]["details"][0]["metadata"]
            fake.exhaust_quota(
                "read", datetime.fromtimestamp(int(metadata["window_start_time"]), UTC)
            )
        with respx.mock(assert_all_called=False) as router:
            fake.mount(router)
            async with httpx.AsyncClient() as http:
                response = await http.request(
                    request["method"],
                    request["url"],
                    params=request["params"],
                    json=_expand(request["body"]),
                    headers={"authorization": f"Bearer {token}"},
                )
        replayed += 1
        if (response.status_code, response.json()) != (expected["status"], expected["body"]):
            differences.append(
                f"{number}: got {response.status_code} {json.dumps(response.json())[:600]}\n"
                f"    recorded {expected['status']} {json.dumps(expected['body'])[:600]}"
            )
    assert not differences, "\n".join(differences)
    assert replayed == 63 - len(NOT_REPLAYED)


def test_every_recording_is_replayed_or_says_why_not() -> None:
    numbers = {number for number, _ in _recordings()}
    assert set(NOT_REPLAYED) <= numbers
    assert len(numbers) == 63
