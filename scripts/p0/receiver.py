"""P0 recording receiver for Razorpay test-mode webhooks (issue #4). Standard library only.

  python scripts/p0/receiver.py [--port 8080] [--fail-first 4]

POST /webhooks/razorpay
  - no x-razorpay-signature header: 400, logged, not saved
  - otherwise: saved to tests/recordings/razorpay/<event-id>-<attempt>.json, then
    500 for attempts 1..N per event id (N = --fail-first), 200 after that.
    A bad signature is logged loudly but still follows the 500/200 schedule, so a
    mistyped secret doesn't lose the retry timings.
GET /pay?order=order_...   a Standard Checkout page for a test order (key id from .env)
GET /                      "p0 receiver up", to tell this receiver from a tunnel's own page

Attempt numbers come from the files already saved, so a restart doesn't reset them.
Every request is also appended to tests/recordings/razorpay/receiver-log.jsonl.
"""

import argparse
import hashlib
import hmac
import html
import json
import os
import pathlib
import re
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "tests" / "recordings" / "razorpay"
LOG = OUT / "receiver-log.jsonl"
DROP_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie"}
LOCK = threading.Lock()


def load_env():
    env = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, value = line.split("=", 1)
                env[name.strip()] = value.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith("RAZORPAY_")})
    return env


ENV = load_env()
SECRET = ENV.get("RAZORPAY_WEBHOOK_SECRET", "").encode()
KEY_ID = ENV.get("RAZORPAY_KEY_ID", "")
if not KEY_ID.startswith("rzp_test_"):
    raise SystemExit("RAZORPAY_KEY_ID does not start with rzp_test_; refusing to run.")
if not SECRET:
    raise SystemExit("RAZORPAY_WEBHOOK_SECRET is empty.")


def now():
    t = time.time()
    return t, datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")


def log(entry):
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class Handler(BaseHTTPRequestHandler):
    fail_first = 4

    def reply(self, code, text, ctype="text/plain; charset=utf-8"):
        body = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-P0-Receiver", "1")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/pay":
            order = parse_qs(url.query).get("order", [""])[0]
            if not re.fullmatch(r"order_[A-Za-z0-9]+", order):
                return self.reply(400, "give ?order=order_...")
            return self.reply(200, checkout_page(order), "text/html; charset=utf-8")
        self.reply(200, "p0 receiver up\n")

    def do_POST(self):
        epoch, iso = now()
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        headers = {
            k: v for k, v in self.headers.items() if k.lower() not in DROP_HEADERS
        }
        event_id = self.headers.get("x-razorpay-event-id", "")
        signature = self.headers.get("x-razorpay-signature", "")
        sha = hashlib.sha256(body).hexdigest()
        entry = {
            "received_at": iso,
            "epoch": round(epoch, 3),
            "path": self.path,
            "event_id": event_id,
            "signature": signature,
            "user_agent": self.headers.get("User-Agent", ""),
            "body_sha256": sha,
            "body_bytes": len(body),
        }
        if self.path != "/webhooks/razorpay":
            entry["answer"] = 404
            log(entry)
            return self.reply(404, "not here\n")
        if not signature:
            entry["answer"] = 400
            log(entry)
            print(f"{iso}  unsigned POST ({len(body)} bytes) -> 400", flush=True)
            return self.reply(400, "unsigned\n")

        expected = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
        entry["signature_valid"] = hmac.compare_digest(expected, signature)
        try:
            entry["event"] = json.loads(body).get("event", "")
        except ValueError:
            entry["event"] = None
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", event_id) or "no-event-id"
        with LOCK:
            attempt = len(list(OUT.glob(f"{safe_id}-*.json"))) + 1
            answer = 500 if attempt <= self.fail_first else 200
            entry.update(attempt=attempt, answer=answer)
            try:
                text, b64 = body.decode("utf-8"), None
            except UnicodeDecodeError:
                import base64

                text, b64 = None, base64.b64encode(body).decode()
            record = {**entry, "headers": headers, "body": text, "body_base64": b64}
            (OUT / f"{safe_id}-{attempt}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            log(entry)
        flag = "" if entry["signature_valid"] else "  SIGNATURE MISMATCH"
        print(
            f"{iso}  {entry['event']}  {event_id}  attempt {attempt} -> {answer}"
            f"  sha256 {sha[:12]}  UA {entry['user_agent']!r}{flag}",
            flush=True,
        )
        self.reply(answer, f"p0 receiver: attempt {attempt}, answered {answer}\n")

    def log_message(self, *args):
        pass


def checkout_page(order):
    return f"""<!doctype html><meta charset="utf-8"><title>P0 test payment</title>
<body style="font:16px system-ui;margin:2rem">
<p>Razorpay <b>test mode</b> order {html.escape(order)}</p>
<button id="pay" style="font-size:1.2rem">Pay</button><pre id="out"></pre>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
const rzp = new Razorpay({{
  key: {json.dumps(KEY_ID)}, order_id: {json.dumps(order)}, name: "wsync P0",
  prefill: {{contact: "9999999999", email: "p0@example.com"}},
  handler: r => document.getElementById("out").textContent = "paid: " + r.razorpay_payment_id
}});
rzp.on("payment.failed", r => document.getElementById("out").textContent =
  "failed: " + r.error.metadata.payment_id + " " + r.error.description);
document.getElementById("pay").onclick = () => rzp.open();
</script>"""


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--fail-first", type=int, default=4)
    a = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    Handler.fail_first = a.fail_first
    print(
        f"p0 receiver on http://localhost:{a.port}  (500 to the first {a.fail_first} attempts)",
        flush=True,
    )
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
