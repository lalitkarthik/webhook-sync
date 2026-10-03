"""An in-memory Google Sheets API v4, served through respx (A18): the start of P5's FakeSheets.

It implements exactly the calls the flusher makes, `spreadsheets.get`, `getByDataFilter` and
`batchUpdate`, with the request kinds it uses (`addSheet`, `updateCells` with values, formats
and notes, `appendDimension`, `updateSheetProperties` for titles and frozen rows), plus the
ones a person's edits need (`deleteSheet`, `sortRange`, `appendCells`). Errors are answered
with the bodies recorded live in `tests/recordings/sheets/`, and `test_fake_sheets_contract.py`
replays those recordings against it so it can't drift from Google.

What it holds, as Google does: tabs addressed by `sheetId`, so a rename is followed; typed
values stored as written and never re-parsed (`=1+1` stays text); numbers as doubles; a batch
applied whole or not at all; reads that clip ranges to the grid and leave out trailing empty
cells and rows; the cell limit counting empty grid cells; 50,000 UTF-16 code units per cell.

It also issues tokens at `oauth2.googleapis.com/token`, so the client's real google-auth
credentials work against it with no Google call.

Not modelled: formulas (never written), A1 ranges and the values API, developer metadata,
quotas that count themselves (`exhaust_quota` sets one), locales other than en_GB.
"""

import copy
import itertools
import json
import random
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal, cast

import httpx
import respx

from webhook_sync.clock import utc_now

SHEETS_HOST = "sheets.googleapis.com"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CELL_LIMIT = 20_000_000
"""Google's cell limit per spreadsheet, empty grid cells included (G22, recording 18)."""
CELL_CHARACTERS = 50_000
"""The most UTF-16 code units one cell holds (recordings 15-17)."""

type Json = dict[str, Any]
type Mask = dict[str, Mask | None]
type Access = Literal["edit", "view", "none"]
type QuotaKind = Literal["read", "write"]

PROJECT_NUMBER = "PROJECT_NUMBER"


class SheetsError(Exception):
    """An error answer: status, message and gRPC status, shaped as Google's error body."""

    def __init__(
        self, code: int, message: str, status: str, details: list[Json] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details

    def body(self) -> Json:
        error: Json = {"code": self.code, "message": self.message, "status": self.status}
        if self.details is not None:
            error["details"] = self.details
        return {"error": error}


def _invalid(message: str) -> SheetsError:
    return SheetsError(400, message, "INVALID_ARGUMENT")


_NOT_FOUND = SheetsError(404, "Requested entity was not found.", "NOT_FOUND")
_DENIED = SheetsError(403, "The caller does not have permission", "PERMISSION_DENIED")
_UNAUTHENTICATED = SheetsError(
    401,
    "Request had invalid authentication credentials. Expected OAuth 2 access token, login "
    "cookie or other valid authentication credential. See "
    "https://developers.google.com/identity/sign-in/web/devconsole-project.",
    "UNAUTHENTICATED",
)
_SERVER_ERRORS = {
    500: ("Internal error encountered.", "INTERNAL"),
    502: ("Bad Gateway", "UNAVAILABLE"),
    503: ("The service is currently unavailable.", "UNAVAILABLE"),
    504: ("The service is currently unavailable.", "DEADLINE_EXCEEDED"),
}


def _quota_error(kind: QuotaKind, window_start: datetime) -> SheetsError:
    """A 429 as recording 51 shows it (no `Retry-After`; the window in the body)."""
    noun = "Read" if kind == "read" else "Write"
    return SheetsError(
        429,
        f"Quota exceeded for quota metric '{noun} requests' and limit '{noun} requests per "
        f"minute per user' of service 'sheets.googleapis.com' for consumer "
        f"'project_number:{PROJECT_NUMBER}'.",
        "RESOURCE_EXHAUSTED",
        [
            {
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "RATE_LIMIT_EXCEEDED",
                "domain": "googleapis.com",
                "metadata": {
                    "consumer": f"projects/{PROJECT_NUMBER}",
                    "quota_metric": f"sheets.googleapis.com/{kind}_requests",
                    "quota_unit": "1/min/{project}/{user}",
                    "service": "sheets.googleapis.com",
                    "quota_limit": f"{noun}RequestsPerMinutePerUser",
                    "quota_limit_value": "60",
                    "window_start_time": str(int(window_start.timestamp())),
                    "quota_location": "global",
                },
            },
            {
                "@type": "type.googleapis.com/google.rpc.Help",
                "links": [
                    {
                        "description": "Request a higher quota limit.",
                        "url": "https://cloud.google.com/docs/quotas/help/request_increase",
                    }
                ],
            },
        ],
    )


# ---------------------------------------------------------------------------------------------
# Field masks


def parse_mask(text: str) -> Mask:
    """A field mask as a tree: `sheets(properties(sheetId),data)` or `sheets.properties`.
    A leaf (None) keeps everything under it."""
    mask, end = _parse_list(text, 0)
    if end != len(text):
        raise ValueError(f"bad field mask: {text!r}")
    return mask


def _parse_list(text: str, at: int) -> tuple[Mask, int]:
    mask: Mask = {}
    while at < len(text) and text[at] != ")":
        match = re.compile(r"[A-Za-z0-9_.*@]+").match(text, at)
        if match is None:
            raise ValueError(f"bad field mask: {text!r}")
        at = match.end()
        sub: Mask | None = None
        if at < len(text) and text[at] == "(":
            sub, at = _parse_list(text, at + 1)
            at += 1  # the ")"
        _merge(mask, match.group().split("."), sub)
        if at < len(text) and text[at] == ",":
            at += 1
    return mask, at


def _merge(mask: Mask, path: list[str], sub: Mask | None) -> None:
    head, rest = path[0], path[1:]
    if not rest:
        existing = mask.get(head, {})
        if sub is None or existing is None:
            mask[head] = None
        else:
            for key, value in sub.items():
                _merge(existing, [key], value)
            mask[head] = existing
        return
    child = mask.setdefault(head, {})
    if child is not None:
        _merge(child, rest, sub)


def apply_mask(value: Any, mask: Mask | None) -> Any:
    """`value` with only what `mask` asks for. Messages left empty are dropped, as Google does;
    list elements are kept, empty or not."""
    if mask is None:
        return value
    if isinstance(value, list):
        return [apply_mask(item, mask) for item in cast(list[Any], value)]
    if not isinstance(value, dict):
        return value
    out: Json = {}
    for key, sub in mask.items():
        if key in value:
            kept = apply_mask(cast(Json, value)[key], sub)
            if kept != {}:
                out[key] = kept
    return out


def _leaf_paths(mask: Mask, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    paths: list[tuple[str, ...]] = []
    for key, sub in mask.items():
        if sub is None:
            paths.append((*prefix, key))
        else:
            paths.extend(_leaf_paths(sub, (*prefix, key)))
    return paths


def _get_path(value: Json, path: Sequence[str]) -> Any:
    current: Any = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = cast(Json, current).get(key)
    return current


def _set_path(target: Json, path: Sequence[str], value: Any) -> None:
    """Set `path` to `value`, or clear it when `value` is None; empty messages are dropped."""
    head, rest = path[0], path[1:]
    if not rest:
        if value is None:
            target.pop(head, None)
        else:
            target[head] = copy.deepcopy(value)
        return
    child = target.setdefault(head, {})
    _set_path(child, rest, value)
    if not child:
        del target[head]


# ---------------------------------------------------------------------------------------------
# Values as Google shows them back


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _effective(entered: Json) -> Json:
    if "formulaValue" in entered:
        raise NotImplementedError("FakeSheets does not evaluate formulas")
    return copy.deepcopy(entered)


_SERIAL_EPOCH = datetime(1899, 12, 30)
_DEFAULT_PATTERNS = {"DATE_TIME": "dd/mm/yyyy hh:mm:ss", "DATE": "dd/mm/yyyy"}
"""What a date format without a pattern shows in an en_GB spreadsheet (recording 54)."""


def _formatted(entered: Json, number_format: Json | None) -> str:
    """The cell as the sheet shows it, for the formats the flusher and the recordings use."""
    if "stringValue" in entered:
        return entered["stringValue"]
    if "boolValue" in entered:
        return "TRUE" if entered["boolValue"] else "FALSE"
    number = float(entered["numberValue"])
    if number_format is None:
        if abs(number) >= 1e15:
            return f"{number:.5E}".replace("E+0", "E+")  # 1.23457E+18 (recording 11)
        return f"{number:.10g}"  # 46297.89513, 42 (recordings 11, 54)
    kind = number_format.get("type")
    pattern = number_format.get("pattern")
    if kind in _DEFAULT_PATTERNS:
        moment = _SERIAL_EPOCH + timedelta(seconds=round(number * 86400))
        return _format_date(moment, pattern or _DEFAULT_PATTERNS[kind])
    match = re.fullmatch(r"(#,##)?0(?:\.(0+))?", pattern or "")
    if kind == "NUMBER" and match is not None:
        decimals = len(match.group(2) or "")
        return f"{number:,.{decimals}f}" if match.group(1) else f"{number:.{decimals}f}"
    raise NotImplementedError(f"FakeSheets can't show the number format {number_format}")


def _format_date(moment: datetime, pattern: str) -> str:
    tokens: list[str] = re.findall(r"yyyy|dd|hh|mm|ss|.", pattern)
    out: list[str] = []
    for i, token in enumerate(tokens):
        if token == "mm":
            minutes = (i >= 2 and tokens[i - 2] == "hh") or (
                i + 2 < len(tokens) and tokens[i + 2] == "ss"
            )
            out.append(f"{moment.minute:02d}" if minutes else f"{moment.month:02d}")
        else:
            out.append(
                {
                    "yyyy": f"{moment.year:04d}",
                    "dd": f"{moment.day:02d}",
                    "hh": f"{moment.hour:02d}",
                    "ss": f"{moment.second:02d}",
                }.get(token, token)
            )
    return "".join(out)


# ---------------------------------------------------------------------------------------------
# The spreadsheet


@dataclass
class Tab:
    """One tab: its properties as Google reports them, and its non-empty cells by (row, col)."""

    properties: Json
    cells: dict[tuple[int, int], Json] = field(default_factory=dict[tuple[int, int], Json])

    @property
    def sheet_id(self) -> int:
        return self.properties["sheetId"]

    @property
    def title(self) -> str:
        return self.properties["title"]

    @property
    def row_count(self) -> int:
        return self.properties["gridProperties"]["rowCount"]

    @property
    def column_count(self) -> int:
        return self.properties["gridProperties"]["columnCount"]

    def cell(self, row: int, column: int) -> Json:
        """The stored cell: `userEnteredValue`, `userEnteredFormat`, `note`; {} when empty."""
        return self.cells.get((row, column), {})

    def value(self, row: int, column: int) -> Any:
        """The cell's typed value (a str, float or bool), or None when it has none."""
        entered: Json = self.cell(row, column).get("userEnteredValue", {})
        return next(iter(entered.values()), None)

    def values(self) -> list[list[Any]]:
        """Every row's typed values, trailing empty cells and rows left out."""
        rows: list[list[Any]] = []
        for row in range(self.row_count):
            values = [self.value(row, column) for column in range(self.column_count)]
            while values and values[-1] is None:
                values.pop()
            rows.append(values)
        while rows and not rows[-1]:
            rows.pop()
        return rows

    def last_used_row(self) -> int:
        """The index of the last row with any cell, or -1."""
        return max((row for row, _ in self.cells), default=-1)

    def show(self, row: int, column: int, *, formatted: bool) -> Json:
        """The cell as a read answers it, before the field mask."""
        stored = self.cell(row, column)
        out: Json = {}
        entered = stored.get("userEnteredValue")
        number_format = stored.get("userEnteredFormat", {}).get("numberFormat")
        if entered:
            out["userEnteredValue"] = entered
            out["effectiveValue"] = _effective(entered)
            if formatted:
                out["formattedValue"] = _formatted(entered, number_format)
        if "userEnteredFormat" in stored:
            out["userEnteredFormat"] = stored["userEnteredFormat"]
        if number_format is not None:
            out["effectiveFormat"] = {"numberFormat": number_format}
        if "note" in stored:
            out["note"] = stored["note"]
        return copy.deepcopy(out)


class FakeSheets:
    """One spreadsheet, as the service account sees it, and the token endpoint.

    It starts as Google makes a new spreadsheet: one tab, `Sheet1` (sheetId 0, 1000 x 26).
    `mount(router)` serves it; `apply(requests)` edits it as a person would (no HTTP, no quota).
    Fault switches: `access` ("edit", "view" or "none": unshared), `deleted`,
    `exhaust_quota`, `fail_next`, `lose_next_write_reply`, `refuse_tokens`, `revoke_tokens`,
    `cell_characters` (a lower per-cell limit, to make one cell bad); `after_next` makes a
    person's edits straight after a call is answered, between the flusher's read and write.
    """

    def __init__(
        self,
        spreadsheet_id: str,
        *,
        properties: Json | None = None,
        access: Access = "edit",
        cell_limit: int = CELL_LIMIT,
        sheet_ids: Iterable[int] = (),
        first_tab: bool = True,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.properties: Json = properties or {
            "title": "webhook",
            "locale": "en_GB",
            "autoRecalc": "ON_CHANGE",
            "timeZone": "Asia/Calcutta",
        }
        self.access: Access = access
        self.deleted = False
        self.cell_limit = cell_limit
        self.tabs: list[Tab] = []
        self.calls: list[str] = []
        """Every Sheets call answered, by name: `get`, `getByDataFilter`, `batchUpdate`."""
        self.token_requests = 0
        self.refuse_tokens = False
        self._sheet_ids = list(sheet_ids)
        self._used_ids: set[int] = set()
        self._random = random.Random(spreadsheet_id)
        self._tokens: set[str] = set()
        self._token_serial = itertools.count(1)
        self._quota: dict[QuotaKind, datetime] = {}
        self._failures: list[int] = []
        self._lose_write_replies = 0
        self.cell_characters = CELL_CHARACTERS
        self._after: list[tuple[str, list[Json]]] = []
        if first_tab:
            self.apply([{"addSheet": {"properties": {"title": "Sheet1", "sheetId": 0}}}])

    # -- what tests set up and look at ------------------------------------------------------

    def tab(self, key: int | str) -> Tab:
        """The tab with this sheetId or title."""
        for tab in self.tabs:
            if key in (tab.sheet_id, tab.title):
                return tab
        raise KeyError(key)

    def has_tab(self, key: int | str) -> bool:
        return any(key in (tab.sheet_id, tab.title) for tab in self.tabs)

    def apply(self, requests: Sequence[Json]) -> list[Json]:
        """Apply a batch as `batchUpdate` does, whole or not at all; the replies. Raises
        `SheetsError` with Google's message when a request fails."""
        tabs = copy.deepcopy(self.tabs)
        replies = [self._request(i, request, tabs) for i, request in enumerate(requests)]
        self.tabs = tabs
        self._used_ids |= {tab.sheet_id for tab in tabs}
        return replies

    def issue_token(self) -> str:
        """A token the fake accepts, as its token endpoint would issue."""
        token = f"fake-token-{next(self._token_serial)}"
        self._tokens.add(token)
        return token

    def revoke_tokens(self) -> None:
        """Every token issued so far answers 401 from now on."""
        self._tokens.clear()

    def exhaust_quota(self, kind: QuotaKind, window_start: datetime) -> None:
        """Calls of this kind answer 429 until `window_start` + 60 s (recording 51)."""
        self._quota[kind] = window_start

    def fail_next(self, *statuses: int) -> None:
        """The next calls answer these 5xx statuses, one each, in order."""
        self._failures.extend(statuses)

    def lose_next_write_reply(self) -> None:
        """The next `batchUpdate` is applied, then its reply lost: the caller times out."""
        self._lose_write_replies += 1

    def after_next(self, name: str, requests: Sequence[Json]) -> None:
        """Apply `requests` as a person's edits right after the next `name` call
        (`get`, `getByDataFilter`, `batchUpdate`) is answered."""
        self._after.append((name, list(requests)))

    def cell_count(self, tabs: Iterable[Tab] | None = None) -> int:
        return sum(tab.row_count * tab.column_count for tab in (tabs or self.tabs))

    # -- serving ----------------------------------------------------------------------------

    def mount(self, router: respx.MockRouter) -> None:
        """Serve the token endpoint and the three Sheets calls on `router`. Calls naming
        another spreadsheet id answer 404, as for an id Google doesn't know."""
        router.post(TOKEN_URL).mock(side_effect=self._token)
        base = r"^/v4/spreadsheets/(?P<sid>[^/:]+)"
        router.get(host=SHEETS_HOST, path__regex=base + "$").mock(side_effect=self._get)
        router.post(host=SHEETS_HOST, path__regex=base + ":getByDataFilter$").mock(
            side_effect=self._get_by_data_filter
        )
        router.post(host=SHEETS_HOST, path__regex=base + ":batchUpdate$").mock(
            side_effect=self._batch_update
        )

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_requests += 1
        if self.refuse_tokens:
            return httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "Invalid JWT Signature."}
            )
        return httpx.Response(
            200,
            json={"access_token": self.issue_token(), "expires_in": 3599, "token_type": "Bearer"},
        )

    def _get(self, request: httpx.Request, sid: str) -> httpx.Response:
        def answer() -> Json:
            if "ranges" in request.url.params or "includeGridData" in request.url.params:
                raise NotImplementedError("FakeSheets serves get without grid data only")
            whole: Json = {
                "spreadsheetId": self.spreadsheet_id,
                "properties": self.properties,
                "sheets": [{"properties": tab.properties} for tab in self.tabs],
                "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{sid}/edit",
            }
            return _masked(whole, request.url.params.get("fields"))

        return self._answer("get", "read", request, sid, answer)

    def _get_by_data_filter(self, request: httpx.Request, sid: str) -> httpx.Response:
        def answer() -> Json:
            body = json.loads(request.content)
            fields = request.url.params.get("fields")
            with_data = fields is not None or bool(body.get("includeGridData"))
            formatted = fields is None or "formattedValue" in fields
            found: dict[int, Json] = {}
            for data_filter in body.get("dataFilters", []):
                grid = data_filter.get("gridRange")
                if grid is None:
                    raise NotImplementedError("FakeSheets reads by gridRange only")
                tab = next((t for t in self.tabs if t.sheet_id == grid.get("sheetId", 0)), None)
                if tab is None:
                    raise _invalid(f"No sheet with id: {grid.get('sheetId', 0)}")
                sheet = found.setdefault(tab.sheet_id, {"properties": tab.properties, "data": []})
                if with_data:
                    sheet["data"].append(_grid_data(tab, grid, formatted=formatted))
            sheets = [found[tab.sheet_id] for tab in self.tabs if tab.sheet_id in found]
            for sheet in sheets:
                if not sheet["data"]:
                    del sheet["data"]
            return _trimmed(_masked({"sheets": sheets}, fields))

        return self._answer("getByDataFilter", "read", request, sid, answer)

    def _batch_update(self, request: httpx.Request, sid: str) -> httpx.Response:
        def answer() -> Json:
            body = json.loads(request.content)
            if self.access != "edit":
                raise _DENIED
            replies = self.apply(body.get("requests", []))
            out: Json = {"spreadsheetId": self.spreadsheet_id, "replies": replies}
            if body.get("includeSpreadsheetInResponse"):
                out["updatedSpreadsheet"] = {
                    "spreadsheetId": self.spreadsheet_id,
                    "properties": self.properties,
                    "sheets": [{"properties": tab.properties} for tab in self.tabs],
                    "spreadsheetUrl": f"https://docs.google.com/spreadsheets/d/{sid}/edit",
                }
            out["commentUpdateState"] = "NO_UPDATES_REQUESTED"
            return copy.deepcopy(out)

        response = self._answer("batchUpdate", "write", request, sid, answer)
        if response.is_success and self._lose_write_replies:
            self._lose_write_replies -= 1
            raise httpx.ReadTimeout("the reply was lost", request=request)
        return response

    def _answer(
        self, name: str, kind: QuotaKind, request: httpx.Request, sid: str, answer: Any
    ) -> httpx.Response:
        try:
            self._admit(kind, request, sid)
            self.calls.append(name)
            response = httpx.Response(200, json=answer())
        except SheetsError as error:
            return httpx.Response(error.code, json=error.body())
        for edit in [e for e in self._after if e[0] == name][:1]:
            self._after.remove(edit)
            self.apply(edit[1])
        return response

    def _admit(self, kind: QuotaKind, request: httpx.Request, sid: str) -> None:
        """The checks before any call: token, spreadsheet, sharing, injected faults, quota."""
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme != "Bearer" or token not in self._tokens:
            raise _UNAUTHENTICATED
        if sid != self.spreadsheet_id or self.deleted:
            raise _NOT_FOUND
        if self.access == "none":
            raise _DENIED
        if self._failures:
            message, status = _SERVER_ERRORS[self._failures[0]]
            raise SheetsError(self._failures.pop(0), message, status)
        window_start = self._quota.get(kind)
        if window_start is not None:
            if utc_now() < window_start + timedelta(seconds=60):
                raise _quota_error(kind, window_start)
            del self._quota[kind]

    # -- batchUpdate requests ----------------------------------------------------------------

    def _request(self, index: int, request: Json, tabs: list[Tab]) -> Json:
        if len(request) != 1:
            raise _invalid(f"Invalid requests[{index}]: exactly one kind of request is allowed")
        kind, spec = next(iter(request.items()))
        handler = {
            "addSheet": self._add_sheet,
            "updateCells": self._update_cells,
            "appendCells": self._append_cells,
            "appendDimension": self._append_dimension,
            "updateSheetProperties": self._update_sheet_properties,
            "deleteSheet": self._delete_sheet,
            "sortRange": self._sort_range,
        }.get(kind)
        if handler is None:
            raise NotImplementedError(f"FakeSheets does not implement {kind}")
        try:
            return handler(spec, tabs)
        except SheetsError as error:
            raise _invalid(f"Invalid requests[{index}].{kind}: {error.message}") from None

    def _grid(self, tabs: list[Tab], sheet_id: int) -> Tab:
        for tab in tabs:
            if tab.sheet_id == sheet_id:
                return tab
        raise _invalid(f"No grid with id: {sheet_id}")

    def _check_cells(self, tabs: list[Tab]) -> None:
        if self.cell_count(tabs) > self.cell_limit:
            raise _invalid(
                "This action would increase the number of cells in the workbook above the "
                f"limit of {self.cell_limit} cells."
            )

    def _new_sheet_id(self, tabs: list[Tab]) -> int:
        taken = self._used_ids | {tab.sheet_id for tab in tabs}
        for sheet_id in self._sheet_ids:
            if sheet_id not in taken:
                return sheet_id
        while True:
            sheet_id = self._random.randrange(1, 2**31)
            if sheet_id not in taken:
                return sheet_id

    def _add_sheet(self, spec: Json, tabs: list[Tab]) -> Json:
        given: Json = copy.deepcopy(spec.get("properties", {}))
        title = given.get("title") or f"Sheet{len(tabs) + 1}"
        if any(tab.title == title for tab in tabs):
            raise _invalid(
                f'A sheet with the name "{title}" already exists. Please enter another name.'
            )
        grid: Json = {"rowCount": 1000, "columnCount": 26} | given.get("gridProperties", {})
        sheet_id = given["sheetId"] if "sheetId" in given else self._new_sheet_id(tabs)
        properties: Json = {
            "sheetId": sheet_id,
            "title": title,
            "index": len(tabs),
            "sheetType": "GRID",
            "gridProperties": {k: v for k, v in grid.items() if v},
        }
        tabs.append(Tab(properties))
        self._check_cells(tabs)
        return {"addSheet": {"properties": copy.deepcopy(properties)}}

    def _delete_sheet(self, spec: Json, tabs: list[Tab]) -> Json:
        tabs.remove(self._grid(tabs, spec.get("sheetId", 0)))
        for index, tab in enumerate(tabs):
            tab.properties["index"] = index
        return {}

    def _update_sheet_properties(self, spec: Json, tabs: list[Tab]) -> Json:
        given: Json = spec.get("properties", {})
        tab = self._grid(tabs, given.get("sheetId", 0))
        for path in _leaf_paths(parse_mask(spec["fields"])):
            if path == ("sheetId",):
                continue
            value = _get_path(given, path)
            if path == ("title",) and any(t.title == value and t is not tab for t in tabs):
                raise _invalid(
                    f'A sheet with the name "{value}" already exists. Please enter another name.'
                )
            _set_path(tab.properties, path, value or None)
        return {}

    def _append_dimension(self, spec: Json, tabs: list[Tab]) -> Json:
        tab = self._grid(tabs, spec.get("sheetId", 0))
        key = "rowCount" if spec.get("dimension") == "ROWS" else "columnCount"
        tab.properties["gridProperties"][key] += spec["length"]
        self._check_cells(tabs)
        return {}

    def _update_cells(self, spec: Json, tabs: list[Tab]) -> Json:
        if "start" not in spec:
            raise NotImplementedError("FakeSheets writes updateCells by start only")
        start: Json = spec["start"]
        tab = self._grid(tabs, start.get("sheetId", 0))
        self._write_rows(tab, start.get("rowIndex", 0), start.get("columnIndex", 0), spec)
        return {}

    def _append_cells(self, spec: Json, tabs: list[Tab]) -> Json:
        tab = self._grid(tabs, spec.get("sheetId", 0))
        first = tab.last_used_row() + 1
        grid = tab.properties["gridProperties"]
        grid["rowCount"] = max(grid["rowCount"], first + len(spec.get("rows", [])))
        self._check_cells(tabs)
        self._write_rows(tab, first, 0, spec)
        return {}

    def _write_rows(self, tab: Tab, first_row: int, first_column: int, spec: Json) -> None:
        fields: str = spec["fields"]
        paths: list[tuple[str, ...]] = (
            [("userEnteredValue",), ("userEnteredFormat",), ("note",)]
            if fields == "*"
            else [tuple(name.split(".")) for name in fields.split(",")]
        )
        for r, row in enumerate(spec.get("rows", [])):
            row_index = first_row + r
            if row_index >= tab.row_count:
                raise _invalid(
                    f"GridCoordinate.rowIndex[{row_index}] is after last row in "
                    f"grid[{tab.row_count - 1}]"
                )
            for c, given in enumerate(row.get("values", [])):
                column_index = first_column + c
                if column_index >= tab.column_count:
                    raise _invalid(
                        f"GridCoordinate.columnIndex[{column_index}] is after last column in "
                        f"grid[{tab.column_count - 1}]"
                    )
                self._write_cell(tab, (row_index, column_index), given, paths)

    def _write_cell(
        self, tab: Tab, at: tuple[int, int], given: Json, paths: list[tuple[str, ...]]
    ) -> None:
        entered = given.get("userEnteredValue", {})
        text = entered.get("stringValue")
        if isinstance(text, str) and _utf16_units(text) > self.cell_characters:
            raise _invalid(
                f"Your input contains more than the maximum of {self.cell_characters} characters "
                "in a single cell."
            )
        if "formulaValue" in entered:
            raise NotImplementedError("FakeSheets does not evaluate formulas")
        cell = copy.deepcopy(tab.cells.get(at, {}))
        for path in paths:
            value = _get_path(given, path)
            if path == ("userEnteredValue",) and value and "numberValue" in value:
                value = {"numberValue": float(value["numberValue"])}  # a double, as Google keeps
            _set_path(cell, path, value)
        if cell:
            tab.cells[at] = cell
        else:
            tab.cells.pop(at, None)

    def _sort_range(self, spec: Json, tabs: list[Tab]) -> Json:
        grid: Json = spec["range"]
        tab = self._grid(tabs, grid.get("sheetId", 0))
        rows = range(
            grid.get("startRowIndex", 0), min(grid.get("endRowIndex", 10**9), tab.row_count)
        )
        columns = range(
            grid.get("startColumnIndex", 0),
            min(grid.get("endColumnIndex", 10**9), tab.column_count),
        )
        lines = [{c: tab.cells[(r, c)] for c in columns if (r, c) in tab.cells} for r in rows]
        for sort in reversed(spec.get("sortSpecs", [])):
            column = columns[0] + sort.get("dimensionIndex", 0)
            descending = sort.get("sortOrder") == "DESCENDING"
            filled = [line for line in lines if column in line]
            empty = [line for line in lines if column not in line]
            filled.sort(key=lambda line: _sort_key(line[column]), reverse=descending)
            lines = filled + empty  # empty cells last, either way
        for r, line in zip(rows, lines, strict=True):
            for c in columns:
                tab.cells.pop((r, c), None)
            for c, cell in line.items():
                tab.cells[(r, c)] = cell
        return {}


def _sort_key(cell: Json) -> tuple[int, Any]:
    entered: Json = cell.get("userEnteredValue", {})
    if "numberValue" in entered:
        return (0, entered["numberValue"])
    if "stringValue" in entered:
        return (1, entered["stringValue"].casefold())
    return (2, entered.get("boolValue", False))


def _grid_data(tab: Tab, grid: Json, *, formatted: bool) -> Json:
    """One range of a tab as `getByDataFilter` answers it: clipped to the grid, every cell in
    full (the mask and the trim come after)."""
    first_row = grid.get("startRowIndex", 0)
    first_column = grid.get("startColumnIndex", 0)
    rows = range(first_row, min(grid.get("endRowIndex", tab.row_count), tab.row_count))
    columns = range(
        first_column, min(grid.get("endColumnIndex", tab.column_count), tab.column_count)
    )
    data: Json = {}
    if first_row:
        data["startRow"] = first_row
    if first_column:
        data["startColumn"] = first_column
    data["rowData"] = [
        {"values": [tab.show(r, c, formatted=formatted) for c in columns]} for r in rows
    ]
    return data


def _masked(answer: Json, fields: str | None) -> Json:
    return answer if fields is None else apply_mask(answer, parse_mask(fields))


def _trimmed(answer: Json) -> Json:
    """Trailing empty cells and rows left out of every range, as Google does (recording 60);
    an empty cell or row before a filled one stays as `{}` (recording 61)."""
    for sheet in answer.get("sheets", []):
        for data in sheet.get("data", []):
            rows: list[Json] = data.get("rowData", [])
            for row in rows:
                values: list[Json] = row.get("values", [])
                while values and not values[-1]:
                    values.pop()
                if not values:
                    row.pop("values", None)
            while rows and not rows[-1]:
                rows.pop()
            if not rows:
                data.pop("rowData", None)
    return answer
