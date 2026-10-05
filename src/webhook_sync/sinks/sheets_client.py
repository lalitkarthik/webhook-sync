"""The Google Sheets client: the Sheets REST API v4 over async httpx (A11).

Three calls, all addressed by the spreadsheet id from the TOML: `get` (tab properties),
`get_by_data_filter` (tabs read by `sheetId` with a field mask) and `batch_update`. Reads have a
10 s timeout and writes 30 s. A call answers the response's JSON object, or a `SheetsFailure`
classed from the live answers (Q141, sheets-sink.md "Failures and their class"):

- 429 → retry, waiting at least until the body's `window_start_time` + 60 s (no `Retry-After`);
- 5xx, timeouts, connection errors → retry;
- 401, 403 → pause the sink;
- 404 → pause, "spreadsheet missing";
- 400 "No grid with id" / "No sheet with id" → pause, "tab missing";
- 400 "... is after last row in grid" → retry (a stale read: the next flush reads again);
- 400 "... above the limit of N cells" → pause, "sheet full";
- any other 400 naming `requests[N]` → dead, with `request_index` N: that request's row.

The bearer token comes from google-auth for the service account (`spreadsheets` scope only),
fetched once and reused until it expires. Nothing here logs: never the token, the spreadsheet id
or the service-account address.
"""

import asyncio
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

import google.auth.exceptions
import google.auth.transport
import httpx
from google.oauth2 import service_account

from webhook_sync.clock import utc_now
from webhook_sync.queue import Failure

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)

READ_TIMEOUT = timedelta(seconds=10)
"""How long `get` and `get_by_data_filter` may take before they are retried (A11)."""

WRITE_TIMEOUT = timedelta(seconds=30)
"""How long `batch_update` may take before it is retried (A11)."""

TOKEN_TIMEOUT = timedelta(seconds=10)
"""How long the token endpoint may take to answer."""

QUOTA_WINDOW = timedelta(seconds=60)
"""Sheets' quotas are per minute: a 429's window ends 60 s after its `window_start_time`."""

TAB_MISSING = "tab missing"
SPREADSHEET_MISSING = "spreadsheet missing"
SHEET_FULL = "sheet full"
KEY_REFUSED = "Google refused the service-account key"

type Json = dict[str, Any]


@dataclass(frozen=True)
class SheetsFailure(Failure):
    """A failed Sheets call, classed. `request_index` is N when a 400 that is neither a missing
    tab, a stale read nor a full sheet names `requests[N]` of the batch: that request's row is
    bad, the rest of the batch can be written again without it (G70)."""

    request_index: int | None = None


class TokenRefused(Exception):
    """Google refused the service-account key: deleted, disabled or never valid."""


class TokenUnavailable(Exception):
    """The token endpoint could not be reached, or answered with a transient error."""


class TokenSource(Protocol):
    async def token(self) -> str: ...

    def forget(self) -> None:
        """Drop the cached token, so the next call fetches a new one."""
        ...


class _HttpxResponse(google.auth.transport.Response):
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    @property
    def status(self) -> int:
        return self._response.status_code

    @property
    def headers(self) -> Mapping[str, str]:
        return self._response.headers

    @property
    def data(self) -> bytes:
        return self._response.content


class _HttpxRequest(google.auth.transport.Request):
    """google-auth's transport over a synchronous httpx call (run off the event loop)."""

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> google.auth.transport.Response:
        try:
            response = httpx.request(
                method,
                url,
                content=body,
                headers=headers,
                timeout=timeout or TOKEN_TIMEOUT.total_seconds(),
            )
        except httpx.HTTPError as error:
            raise google.auth.exceptions.TransportError(type(error).__name__) from error
        return _HttpxResponse(response)


class ServiceAccountTokens:
    """Bearer tokens for the service account in `GOOGLE_SERVICE_ACCOUNT_B64`, `spreadsheets`
    scope only. A token is fetched once and reused until google-auth sees it expire."""

    def __init__(self, info: Mapping[str, Any]) -> None:
        self._credentials = service_account.Credentials.from_service_account_info(  # pyright: ignore[reportUnknownMemberType]
            dict(info), scopes=list(SCOPES)
        )
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        async with self._lock:
            if not self._credentials.valid:
                await asyncio.to_thread(self._refresh)
            return cast(str, self._credentials.token)  # pyright: ignore[reportUnknownMemberType]

    def forget(self) -> None:
        self._credentials.token = None

    def _refresh(self) -> None:
        try:
            self._credentials.refresh(_HttpxRequest())  # pyright: ignore[reportUnknownMemberType]
        except google.auth.exceptions.RefreshError as error:
            if error.retryable:  # pyright: ignore[reportUnknownMemberType]
                raise TokenUnavailable(type(error).__name__) from error
            raise TokenRefused(type(error).__name__) from error
        except google.auth.exceptions.TransportError as error:
            raise TokenUnavailable(type(error).__name__) from error


class SheetsClient:
    """One spreadsheet's three calls. `http` is the caller's client; its lifetime is theirs."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        spreadsheet_id: str,
        tokens: TokenSource,
        *,
        api_url: str = SHEETS_API,
        read_timeout: timedelta = READ_TIMEOUT,
        write_timeout: timedelta = WRITE_TIMEOUT,
    ) -> None:
        self._http = http
        self._url = f"{api_url}/{spreadsheet_id}"
        self._tokens = tokens
        self._read_timeout = read_timeout
        self._write_timeout = write_timeout

    async def get(self, *, fields: str) -> Json | SheetsFailure:
        """`spreadsheets.get` with a field mask, e.g. `sheets.properties` for every tab."""
        return await self._call(
            "GET", self._url, params={"fields": fields}, limit=self._read_timeout
        )

    async def get_by_data_filter(
        self, data_filters: Sequence[Mapping[str, Any]], *, fields: str
    ) -> Json | SheetsFailure:
        """`spreadsheets.getByDataFilter`: the ranges in `data_filters` (by `sheetId`, so a
        renamed tab is followed), with only what `fields` asks for."""
        return await self._call(
            "POST",
            f"{self._url}:getByDataFilter",
            params={"fields": fields},
            body={"dataFilters": list(data_filters)},
            limit=self._read_timeout,
        )

    async def batch_update(self, requests: Sequence[Mapping[str, Any]]) -> Json | SheetsFailure:
        """`spreadsheets.batchUpdate`: every request applied in order, or none of them."""
        return await self._call(
            "POST",
            f"{self._url}:batchUpdate",
            body={"requests": list(requests)},
            limit=self._write_timeout,
        )

    async def _call(
        self,
        method: str,
        url: str,
        *,
        limit: timedelta,
        params: Mapping[str, str] | None = None,
        body: Json | None = None,
    ) -> Json | SheetsFailure:
        try:
            token = await self._tokens.token()
        except TokenRefused:
            return SheetsFailure(KEY_REFUSED, "pause", pause_target="sink")
        except TokenUnavailable as error:
            return SheetsFailure(f"Google token call failed: {error}", "retry")
        try:
            # The whole call, not only each read, ends at the timeout: a trickle can't hang it.
            async with asyncio.timeout(limit.total_seconds()):
                response = await self._http.request(
                    method,
                    url,
                    params=params,
                    json=body,
                    headers={"authorization": f"Bearer {token}"},
                    timeout=limit.total_seconds(),
                )
        except TimeoutError:
            return SheetsFailure("Sheets call failed: no answer within the timeout", "retry")
        except httpx.TransportError as error:  # a timeout, a refused or dropped connection
            return SheetsFailure(f"Sheets call failed: {type(error).__name__}", "retry")
        try:
            answer: object = response.json()
        except ValueError:
            answer = None
        if response.is_success:
            if isinstance(answer, dict):
                return cast(Json, answer)
            return SheetsFailure(
                f"Sheets answered HTTP {response.status_code} with a body that is not a JSON "
                "object",
                "retry",
                http_status=response.status_code,
            )
        if response.status_code == 401:
            self._tokens.forget()
        return classify(response.status_code, answer, now=utc_now())


_REQUEST_INDEX = re.compile(r"requests\[(\d+)\]")
_TAB_MISSING = re.compile(r"No (grid|sheet) with id")
_STALE_READ = "after last row in grid"
_CELL_LIMIT = re.compile(r"above the limit of \d+ cells")


def classify(status: int, body: object, *, now: datetime) -> SheetsFailure:
    """What a Sheets error answer means (Q141). `body` is the parsed JSON, or None."""
    error = _object(_object(body).get("error"))
    message = error.get("message")
    message = message if isinstance(message, str) else ""
    said = f"Sheets answered HTTP {status}"
    if message:
        said = f"{said}: {message[:200]}"
    if status == 429:
        return SheetsFailure(
            said, "retry", retry_after=_quota_wait(error, now=now), http_status=status
        )
    if status >= 500:
        return SheetsFailure(said, "retry", http_status=status)
    if status in (401, 403):
        return SheetsFailure(said, "pause", pause_target="sink", http_status=status)
    if status == 404:
        return SheetsFailure(SPREADSHEET_MISSING, "pause", pause_target="sink", http_status=status)
    if status == 400:
        if _TAB_MISSING.search(message):
            return SheetsFailure(TAB_MISSING, "pause", pause_target="sink", http_status=status)
        if _STALE_READ in message:
            return SheetsFailure(said, "retry", http_status=status)
        if _CELL_LIMIT.search(message):
            return SheetsFailure(SHEET_FULL, "pause", pause_target="sink", http_status=status)
        index = _REQUEST_INDEX.search(message)
        if index is not None:
            return SheetsFailure(
                said, "dead", http_status=status, request_index=int(index.group(1))
            )
    return SheetsFailure(said, "dead", http_status=status)


def _quota_wait(error: Mapping[str, Any], *, now: datetime) -> timedelta | None:
    """The wait until a 429's quota window ends: `window_start_time` + 60 s, from now. None
    when the body names no window."""
    details: object = error.get("details")
    if not isinstance(details, list):
        return None
    for detail in cast(list[object], details):
        start = _object(_object(detail).get("metadata")).get("window_start_time")
        if isinstance(start, str) and start.isdigit():
            ends = datetime.fromtimestamp(int(start), UTC) + QUOTA_WINDOW
            return max(ends - now, timedelta(0))
    return None


def _object(value: object) -> Mapping[str, Any]:
    return cast(Mapping[str, Any], value) if isinstance(value, dict) else {}
