"""Razorpay's API and Google's token endpoint over real HTTP on 127.0.0.1, for a `wsync serve`
running as a subprocess (respx only reaches the test's own process).

- `GET /v1/payments/{id}` answers the recorded fetch for that id from
  `tests/recordings/razorpay/fetch/payment-*-{id}.json`, or the recorded 400 "The id provided
  does not exist" for any other id.
- `GET /v1/payments/{id}/refunds` answers an empty page, as Razorpay does for a payment with
  no refunds.
- `POST /token` refuses the key with 400 `invalid_grant`, as Google does for a deleted key: the
  flusher pauses the sheets sink, "Google refused the service-account key". Point the fixture
  service account's `token_uri` here (`token_uri(port)`) and no call ever reaches Google.

The smoke test and the verify skill (`.claude/skills/verify-webhook-sync/`) both start it.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any, ClassVar, override
from urllib.parse import urlsplit

FETCH_RECORDINGS = Path(__file__).parent.parent / "recordings" / "razorpay" / "fetch"
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


class _Handler(BaseHTTPRequestHandler):
    payments: ClassVar[dict[str, tuple[int, Any]]] = {}

    def do_GET(self) -> None:
        parts = urlsplit(self.path).path.strip("/").split("/")
        if parts[:2] == ["v1", "payments"] and len(parts) == 4 and parts[3] == "refunds":
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

    def _answer(self, status: int, body: Any) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    @override
    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"upstreams: {format % args}\n")
        sys.stderr.flush()


def start_upstreams(port: int = 0) -> ThreadingHTTPServer:
    """Serve the fakes on 127.0.0.1:`port` (0: a free one) in a daemon thread; the caller calls
    `shutdown()` and `server_close()` when done. The bound port is `server_address[1]`."""
    handler = type("Handler", (_Handler,), {"payments": recorded_payments()})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server
