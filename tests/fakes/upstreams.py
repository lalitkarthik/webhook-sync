"""Razorpay's API and Google's token endpoint over real HTTP on 127.0.0.1, for a `wsync serve`
running as a subprocess (respx only reaches the test's own process).

- `GET /v1/payments/{id}` answers the recorded fetch for that id from
  `tests/recordings/razorpay/fetch/payment-*-{id}.json`, or the recorded 400 "The id provided
  does not exist" for any other id.
- `GET /v1/payments/{id}/refunds` answers an empty page, as Razorpay does for a payment with
  no refunds.
- `GET /v1/payments` and `GET /v1/refunds`, only when `start_upstreams` is given `listed_at`,
  answer as Razorpay's lists do (newest first, `from` and `to` inclusive, `count` after `skip`,
  now in the `Date` header) from the recorded list answers, every `created_at` moved by the same
  amount so the newest payment was created at `listed_at`; each waits `list_delay` seconds first.
  Without `listed_at` they answer 404, as before.
- `POST /token` refuses the key with 400 `invalid_grant`, as Google does for a deleted key: the
  flusher pauses the sheets sink, "Google refused the service-account key". Point the fixture
  service account's `token_uri` here (`token_uri(port)`) and no call ever reaches Google.

The smoke test and the verify skill (`.claude/skills/verify-webhook-sync/`) both start it.
"""

import json
import sys
import time
from datetime import datetime
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, ClassVar, override
from urllib.parse import parse_qs, urlsplit

FETCH_RECORDINGS = Path(__file__).parent.parent / "recordings" / "razorpay" / "fetch"
LIST_RECORDINGS = Path(__file__).parent.parent / "recordings" / "razorpay" / "api" / "list"
EMPTY_REFUNDS: dict[str, Any] = {"entity": "collection", "count": 0, "items": []}
REFUSED_KEY = {"error": "invalid_grant", "error_description": "Invalid JWT Signature."}


def token_uri(port: int) -> str:
    return f"http://127.0.0.1:{port}/token"


def recorded_payments() -> dict[str, tuple[int, Any]]:
    """Each recorded payment id with its recorded status and body."""
    answers: dict[str, tuple[int, Any]] = {}
    for path in sorted(FETCH_RECORDINGS.glob("payment-*-pay_*.json")):
        recording = json.loads(path.read_text(encoding="utf-8"))
        answers[recording["body"]["id"]] = (recording["http"], recording["body"])
    return answers


def _not_found() -> tuple[int, Any]:
    recording = json.loads((FETCH_RECORDINGS / "not-found-well-formed.json").read_text("utf-8"))
    return recording["http"], recording["body"]


def recorded_list(name: str, shift: int) -> list[dict[str, Any]]:
    """A recorded list answer's items, each `created_at` moved by `shift` seconds."""
    path = LIST_RECORDINGS / f"{name}-list-count100.json"
    items: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["body"]["items"]
    return [item | {"created_at": item["created_at"] + shift} for item in items]


class _Handler(BaseHTTPRequestHandler):
    payments: ClassVar[dict[str, tuple[int, Any]]] = {}
    lists: ClassVar[dict[str, list[dict[str, Any]]] | None] = None
    list_delay: ClassVar[float] = 0.0
    list_calls: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        parts = url.path.strip("/").split("/")
        if self.lists is not None and len(parts) == 2 and parts[1] in self.lists:
            self.list_calls.append(parts[1])
            time.sleep(self.list_delay)
            self._answer_list(self.lists[parts[1]], parse_qs(url.query))
        elif parts[:2] == ["v1", "payments"] and len(parts) == 4 and parts[3] == "refunds":
            self._answer(200, EMPTY_REFUNDS)
        elif parts[:2] == ["v1", "payments"] and len(parts) == 3:
            self._answer(*self.payments.get(parts[2], _not_found()))
        else:
            self._answer(404, {"error": "not faked"})

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("content-length") or 0))
        if urlsplit(self.path).path == "/token":
            self._answer(400, REFUSED_KEY)
        else:
            self._answer(404, {"error": "not faked"})

    def _answer_list(self, items: list[dict[str, Any]], query: dict[str, list[str]]) -> None:
        start, end = int(query["from"][0]), int(query["to"][0])
        count, skip = int(query.get("count", ["10"])[0]), int(query.get("skip", ["0"])[0])
        newest_first = sorted(items, key=lambda item: item["created_at"], reverse=True)
        page = [item for item in newest_first if start <= item["created_at"] <= end]
        page = page[skip : skip + count]
        body = {"entity": "collection", "count": len(page), "items": page}
        self._answer(200, body, {"date": formatdate(usegmt=True)})

    def _answer(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    @override
    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"upstreams: {format % args}\n")
        sys.stderr.flush()


def start_upstreams(
    port: int = 0, *, listed_at: datetime | None = None, list_delay: float = 0.0
) -> ThreadingHTTPServer:
    """Serve the fakes on 127.0.0.1:`port` (0: a free one) in a daemon thread; the caller calls
    `shutdown()` and `server_close()` when done. The bound port is `server_address[1]`; the list
    calls answered so far are `list_calls(server)`."""
    lists = None
    if listed_at is not None:
        newest = max(item["created_at"] for item in recorded_list("payments", 0))
        shift = int(listed_at.timestamp()) - newest
        lists = {name: recorded_list(name, shift) for name in ("payments", "refunds")}
    handler = type(
        "Handler",
        (_Handler,),
        {
            "payments": recorded_payments(),
            "lists": lists,
            "list_delay": list_delay,
            "list_calls": [],
        },
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def list_calls(server: ThreadingHTTPServer) -> list[str]:
    """The lists called so far, in order: `payments` or `refunds`."""
    handler: type[_Handler] = server.RequestHandlerClass  # pyright: ignore[reportAssignmentType]
    return handler.list_calls
