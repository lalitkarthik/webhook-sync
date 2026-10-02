"""Razorpay test-mode API calls for P0 (issue #4). Never prints a key.

  python scripts/p0/rzp.py order <paise> [manual]         create an order (manual: payment_capture 0); prints its checkout URL
  python scripts/p0/rzp.py get <pay_id>                   fetch a payment (status, fee, tax)
  python scripts/p0/rzp.py capture <pay_id> <paise>
  python scripts/p0/rzp.py refund <pay_id> <paise> [optimum]
  python scripts/p0/rzp.py refund-get <rfnd_id>
  python scripts/p0/rzp.py list [count]                   GET /v1/payments

Every response is saved under tests/recordings/razorpay/api/.
"""

import base64
import json
import sys
import time
import urllib.error
import urllib.request

from receiver import ENV, KEY_ID, OUT  # receiver refuses a non-test key on import

API = OUT / "api"
AUTH = (
    "Basic "
    + base64.b64encode(f"{KEY_ID}:{ENV['RAZORPAY_KEY_SECRET']}".encode()).decode()
)
SHOW = (
    "id",
    "entity",
    "status",
    "amount",
    "fee",
    "tax",
    "method",
    "captured",
    "order_id",
    "amount_refunded",
    "refund_status",
    "speed_requested",
    "speed_processed",
    "created_at",
    "error_code",
    "error_description",
)


def call(method, path, data=None, name=None):
    req = urllib.request.Request(
        "https://api.razorpay.com/v1" + path,
        data=json.dumps(data).encode() if data is not None else None,
        method=method,
        headers={"Authorization": AUTH, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            code, body = r.status, json.load(r)
    except urllib.error.HTTPError as e:
        code, body = e.code, json.load(e)
    API.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    tag = name or body.get("id") or path.strip("/").replace("/", "_")
    status = body.get("status", "")
    out = API / f"{stamp}-{tag}{'-' + status if status else ''}.json"
    out.write_text(
        json.dumps(
            {
                "request": f"{method} {path}",
                "http": code,
                "at": time.time(),
                "body": body,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"HTTP {code}  saved {out.relative_to(OUT.parents[2])}")
    shown = {k: body[k] for k in SHOW if k in body} or body
    print(json.dumps(shown, indent=2))
    return body


def main():
    a = sys.argv[1:]
    if a[:1] == ["order"]:
        data = {
            "amount": int(a[1]),
            "currency": "INR",
            "receipt": f"p0-{int(time.time())}",
        }
        if a[2:3] == ["manual"]:
            data["payment_capture"] = 0
        o = call("POST", "/orders", data)
        print(f"\nOpen: http://localhost:8080/pay?order={o.get('id')}")
    elif a[:1] == ["get"]:
        call("GET", f"/payments/{a[1]}")
    elif a[:1] == ["capture"]:
        call(
            "POST",
            f"/payments/{a[1]}/capture",
            {"amount": int(a[2]), "currency": "INR"},
        )
    elif a[:1] == ["refund"]:
        data = {"amount": int(a[2])}
        if len(a) > 3:
            data["speed"] = a[3]
        call("POST", f"/payments/{a[1]}/refund", data)
    elif a[:1] == ["refund-get"]:
        call("GET", f"/refunds/{a[1]}")
    elif a[:1] == ["list"]:
        b = call(
            "GET", f"/payments?count={a[1] if len(a) > 1 else 10}", name="payments-list"
        )
        for p in b.get("items", []):
            print(p["id"], p["status"], p["created_at"])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
