"""Record Razorpay's fetch answers from test mode for P2 (issue #20). Standard library only.

  python scripts/p2/record_fetch.py live      GET the owner's test payments and save each answer
  python scripts/p2/record_fetch.py samples   save Razorpay's documented samples, marked unrecorded

`live` lists the account's payments (GET /v1/payments?count=100), then for each one makes the
fetch P2 makes, GET /v1/payments/{id}?expand[]=refunds&expand[]=card. It also records a refunds
page (GET /v1/payments/{id}/refunds?count=1, and skip=1 for the next page) for a payment with
refunds, and the not-found answers for pay_00000000000000 and abc. GET only: nothing is created,
captured or refunded.

Keys come from RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in the environment (or .env). The key id
must start rzp_test_, or the script refuses to run. No key is ever printed or saved: the script
prints only file names, ids and statuses. Set SSL_CERT_FILE when a proxy needs its own CA.

Every saved body is redacted first: `email` becomes a placeholder, `contact` and `vpa` are
removed, card `last4` and `name` are removed, acquirer references are blanked, and any email
or phone-like value in `notes` is replaced or removed.

Files go to tests/recordings/razorpay/fetch/.
"""

import base64
import json
import os
import pathlib
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
OUT = ROOT / "tests" / "recordings" / "razorpay" / "fetch"
API = "https://api.razorpay.com/v1"
EXPAND = "expand[]=refunds&expand[]=card"
EMAIL_PLACEHOLDER = "customer@example.com"
REDACTED = "<redacted>"
KEEP_HEADERS = ("content-type", "date", "retry-after", "uber-trace-id")
ACQUIRER_REFS = (
    "rrn",
    "arn",
    "auth_code",
    "bank_transaction_id",
    "upi_transaction_id",
    "transaction_id",
    "authentication_reference_number",
)
EMAIL_RE = re.compile(r"[^@\s\"]+@[^@\s\"]+")
PHONE_RE = re.compile(r"^\+?\d[\d\s-]{8,}$")


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                name, value = line.split("=", 1)
                env[name.strip()] = value.strip()
    env.update({k: v for k, v in os.environ.items() if k.startswith("RAZORPAY_")})
    return env


def redact(value: Any, key: str = "") -> Any:
    """Return a copy with personal and instrument data replaced or removed."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k in ("contact", "vpa"):
                continue
            if key == "card" and k in ("last4", "name"):
                continue
            if k == "email":
                out[k] = EMAIL_PLACEHOLDER if v else v
            elif key == "acquirer_data" and k in ACQUIRER_REFS:
                out[k] = REDACTED if v else v
            elif key == "notes" and isinstance(v, str) and PHONE_RE.match(v):
                continue
            elif key == "notes" and isinstance(v, str) and EMAIL_RE.search(v):
                out[k] = EMAIL_PLACEHOLDER
            else:
                out[k] = redact(v, k)
        return out
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    return value


def save(name: str, record: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"saved {path.relative_to(ROOT)}")


class Client:
    def __init__(self) -> None:
        env = load_env()
        key_id = env.get("RAZORPAY_KEY_ID", "")
        secret = env.get("RAZORPAY_KEY_SECRET", "")
        if not key_id.startswith("rzp_test_"):
            raise SystemExit("RAZORPAY_KEY_ID does not start with rzp_test_; refusing to run.")
        if not secret:
            raise SystemExit("RAZORPAY_KEY_SECRET is empty.")
        token = base64.b64encode(f"{key_id}:{secret}".encode()).decode()
        self._auth = f"Basic {token}"
        cafile = os.environ.get("SSL_CERT_FILE")
        self._ctx = ssl.create_default_context(cafile=cafile) if cafile else None

    def get(self, path: str) -> dict[str, Any]:
        """GET one path; return the recording (request, status, kept headers, body)."""
        req = urllib.request.Request(
            API + path, method="GET", headers={"Authorization": self._auth}
        )
        try:
            with urllib.request.urlopen(req, timeout=30, context=self._ctx) as r:
                status, headers, raw = r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            status, headers, raw = e.code, e.headers, e.read()
        try:
            body: Any = json.loads(raw)
        except ValueError:
            body = raw.decode("utf-8", "replace")
        kept = {h: headers[h] for h in KEEP_HEADERS if headers.get(h) is not None}
        return {
            "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "request": f"GET /v1{path}",
            "http": status,
            "headers": kept,
            "body": redact(body),
        }


def kind(p: dict[str, Any]) -> str:
    status = p["status"]
    if status == "captured" and p.get("refund_status") == "partial":
        return "captured-partly-refunded"
    if status == "captured" and p.get("refund_status") == "full":
        return "captured-fully-refunded"
    return status


def live() -> None:
    c = Client()
    listing = c.get("/payments?count=100")
    save("payments-list", listing)
    payments: list[dict[str, Any]] = listing["body"].get("items", [])
    with_refunds = None
    for p in payments:
        rec = c.get(f"/payments/{p['id']}?{EXPAND}")
        body = rec["body"]
        save(f"payment-{kind(body)}-{p['id']}", rec)
        print(f"  {p['id']} {body.get('status')} http {rec['http']}")
        if (body.get("refunds") or {}).get("count") and with_refunds is None:
            with_refunds = p["id"]
        time.sleep(0.2)
    if with_refunds:
        save("refunds-page-1", c.get(f"/payments/{with_refunds}/refunds?count=1"))
        save("refunds-page-2", c.get(f"/payments/{with_refunds}/refunds?count=1&skip=1"))
        save("refunds-page-3-empty", c.get(f"/payments/{with_refunds}/refunds?count=1&skip=2"))
    save("not-found-well-formed", c.get(f"/payments/pay_00000000000000?{EXPAND}"))
    save("not-found-malformed", c.get(f"/payments/abc?{EXPAND}"))
    save("refunds-unknown-payment", c.get("/payments/pay_00000000000000/refunds?count=1"))


# Razorpay's documented samples for answers test mode can't produce here. Each is saved as
# documented apart from redaction; `note` says what, if anything, was put together.
FETCH = "https://razorpay.com/docs/api/payments/fetch-with-id/"
EXPANDED_CARD = "https://razorpay.com/docs/api/payments/fetch-payment-expanded-card/"
INTERNATIONAL = "https://razorpay.com/docs/payments/international-payments/currency-conversion/"
REFUND_WEBHOOKS = "https://razorpay.com/docs/webhooks/refunds/"
CAPTURE_SETTINGS = "https://razorpay.com/docs/payments/payments/capture-settings/"

SAMPLES: dict[str, dict[str, Any]] = {
    "unrecorded-international-captured": {
        "source": INTERNATIONAL,
        "note": "Payment Entity sample for a $1 payment. Documented without `captured` or "
        "`card`; fee and tax are in base_currency (paise).",
        "body": {
            "id": "pay_Donb2t6WkJYhfU",
            "entity": "payment",
            "amount": 100,
            "currency": "USD",
            "base_amount": 7129,
            "base_currency": "INR",
            "status": "captured",
            "order_id": "order_CjCr5oKh4AVC51",
            "international": True,
            "method": "card",
            "amount_refunded": 0,
            "refund_status": None,
            "description": "Payment at the Dollar Store",
            "card_id": "card_Donb2wk8eC8EDN",
            "email": "<email>",
            "contact": "<phone>",
            "notes": [],
            "fee": 207,
            "tax": 0,
            "error_code": None,
            "error_description": None,
            "created_at": 1400826750,
        },
    },
    "unrecorded-card-captured-expanded": {
        "source": FETCH,
        "note": "The fetch page's expanded-card Success sample (?expand[]=card): a captured "
        "Visa debit payment.",
        "body": {
            "id": "pay_DG4ZdRK8ZnXC3k",
            "entity": "payment",
            "amount": 5000,
            "currency": "INR",
            "status": "captured",
            "order_id": None,
            "invoice_id": None,
            "international": False,
            "method": "card",
            "amount_refunded": 0,
            "refund_status": None,
            "captured": True,
            "description": "Test Transaction",
            "card_id": "card_LPpN6ubeosLH4g",
            "card": {
                "id": "card_LPpN6ubeosLH4g",
                "entity": "card",
                "name": "",
                "last4": "0153",
                "network": "Visa",
                "type": "debit",
                "issuer": None,
                "international": False,
                "sub_type": "consumer",
                "token_iin": None,
            },
            "bank": None,
            "wallet": None,
            "email": "<email>",
            "contact": "<phone>",
            "notes": {"address": "Corporate Office"},
            "fee": 100,
            "tax": 0,
            "error_code": None,
            "error_description": None,
            "error_source": None,
            "error_step": None,
            "error_reason": None,
            "acquirer_data": {"auth_code": "878694"},
            "created_at": 1678452635,
        },
    },
    "unrecorded-card-failed-expanded": {
        "source": EXPANDED_CARD,
        "note": "The expanded-card page's Success sample: a failed RuPay credit payment whose "
        "notes carry an email and phone (redacted here).",
        "body": {
            "id": "pay_H9oR0gLCaVlV6m",
            "entity": "payment",
            "amount": 100,
            "currency": "INR",
            "status": "failed",
            "order_id": "order_H9o58N6qmLYQKC",
            "invoice_id": None,
            "terminal_id": "term_G5kJnYM9GhhLYT",
            "international": False,
            "method": "card",
            "amount_refunded": 0,
            "refund_status": None,
            "captured": False,
            "description": None,
            "card_id": "card_H9oR0ocen1cmZq",
            "card": {
                "id": "card_H9oR0ocen1cmZq",
                "entity": "card",
                "name": "Gaurav",
                "last4": "1213",
                "network": "RuPay",
                "type": "credit",
                "issuer": "UTIB",
                "international": False,
                "emi": False,
                "sub_type": "business",
            },
            "bank": None,
            "wallet": None,
            "vpa": None,
            "email": "gaurav.kumar@example.com",
            "contact": "+919000090000",
            "notes": {"email": "gaurav.kumar@example.com", "phone": "09000090000"},
            "fee": None,
            "tax": None,
            "error_code": "BAD_REQUEST_ERROR",
            "error_description": "Card issuer is invalid",
            "error_source": "customer",
            "error_step": "payment_authentication",
            "error_reason": "incorrect_card_details",
            "acquirer_data": {
                "auth_code": None,
                "authentication_reference_number": "100222021120200000000742753928",
            },
            "created_at": 1620807547,
        },
    },
    "unrecorded-upi-captured": {
        "source": FETCH,
        "note": "The fetch page's UPI sample: `email` null, `upi` inline (its vpa removed).",
        "body": {
            "id": "pay_MT3PgKGazkDUUM",
            "entity": "payment",
            "amount": 1000,
            "currency": "INR",
            "status": "captured",
            "order_id": "order_MLXS6VLbhKH7i1",
            "invoice_id": "inv_MLXS5jI0oFNX2a",
            "international": False,
            "method": "upi",
            "amount_refunded": 0,
            "refund_status": None,
            "captured": True,
            "description": "#MLXS5jI0oFNX2a",
            "card_id": None,
            "bank": None,
            "wallet": None,
            "vpa": "gaurav.kumar@examplebank",
            "email": None,
            "contact": "+919000090000",
            "notes": [],
            "fee": 24,
            "tax": 4,
            "error_code": None,
            "error_description": None,
            "error_source": None,
            "error_step": None,
            "error_reason": None,
            "acquirer_data": {"rrn": "322398320067"},
            "created_at": 1692694191,
            "upi": {
                "payer_account_type": "credit_card",
                "vpa": "gaurav.kumar@examplebank",
                "flow": "intent",
            },
        },
    },
    "unrecorded-wallet-captured": {
        "source": FETCH,
        "note": "The fetch page's Wallet sample (airtelmoney).",
        "body": {
            "id": "pay_MT4GRFUHBfMCNf",
            "entity": "payment",
            "amount": 250000,
            "currency": "INR",
            "status": "captured",
            "order_id": "order_MT4FE4i64KutqB",
            "invoice_id": None,
            "international": False,
            "method": "wallet",
            "amount_refunded": 0,
            "refund_status": None,
            "captured": True,
            "description": "#MT4F1XRgHKcWEt",
            "card_id": None,
            "bank": None,
            "wallet": "airtelmoney",
            "vpa": None,
            "email": "gaurav.kumar@example.com",
            "contact": "+919000090000",
            "notes": [],
            "fee": 5900,
            "tax": 900,
            "error_code": None,
            "error_description": None,
            "error_source": None,
            "error_step": None,
            "error_reason": None,
            "acquirer_data": {"transaction_id": None},
            "created_at": 1692697187,
        },
    },
    "unrecorded-refund-failed": {
        "source": REFUND_WEBHOOKS,
        "note": "Put together from the refund.failed webhook sample: its payment entity, with "
        "its failed refund placed in `refunds` as ?expand[]=refunds returns it. The sample's "
        "amount_refunded (190000) and refund_status (partial) are the same snapshot as the "
        "other refund samples, so they don't show what a failed refund does to those fields.",
        "body": {
            "id": "pay_FPoJKWQQ8lK13n",
            "entity": "payment",
            "amount": 500000,
            "currency": "INR",
            "base_amount": 500000,
            "status": "captured",
            "order_id": "order_FPoIeimWki9j8A",
            "invoice_id": None,
            "international": False,
            "method": "netbanking",
            "amount_refunded": 190000,
            "amount_transferred": 0,
            "refund_status": "partial",
            "captured": True,
            "description": None,
            "card_id": None,
            "bank": "HDFC",
            "wallet": None,
            "vpa": None,
            "email": "gaurav.kumar@example.com",
            "contact": "+919000090000",
            "notes": [],
            "fee": 11800,
            "tax": 1800,
            "error_code": None,
            "error_description": None,
            "error_source": None,
            "error_step": None,
            "error_reason": None,
            "acquirer_data": {"bank_transaction_id": "4827433"},
            "created_at": 1597226379,
            "refunds": {
                "entity": "collection",
                "count": 1,
                "items": [
                    {
                        "id": "rfnd_FS8TWyPrCsa0OB",
                        "entity": "refund",
                        "amount": 50000,
                        "currency": "INR",
                        "payment_id": "pay_FPoJKWQQ8lK13n",
                        "notes": {"comment": "Customer Notes for Webhooks."},
                        "receipt": None,
                        "acquirer_data": {"arn": None},
                        "created_at": 1597734071,
                        "batch_id": None,
                        "status": "failed",
                        "speed_processed": "normal",
                        "speed_requested": "optimum",
                    }
                ],
            },
        },
    },
    "unrecorded-authorized-auto-refunded": {
        "source": CAPTURE_SETTINGS,
        "note": "CONSTRUCTED, not a Razorpay sample: no page shows an uncaptured payment after "
        "its auto-refund (G66, spike S-Q7a). The source documents only that uncaptured "
        "payments are refunded automatically. This body is the fetch page's Netbanking "
        "sample shape with status refunded, captured false, fee and tax null, "
        "amount_refunded = amount and refund_status full, as the canceled rule (A7, A22) "
        "assumes. Replace it with a recording once S-Q7a runs.",
        "body": {
            "id": "pay_UnrecordedAutoRf",
            "entity": "payment",
            "amount": 50000,
            "currency": "INR",
            "status": "refunded",
            "order_id": "order_UnrecordedAuto",
            "invoice_id": None,
            "international": False,
            "method": "netbanking",
            "amount_refunded": 50000,
            "refund_status": "full",
            "captured": False,
            "description": None,
            "card_id": None,
            "card": None,
            "bank": "HDFC",
            "wallet": None,
            "vpa": None,
            "email": "gaurav.kumar@example.com",
            "contact": "+919000090000",
            "notes": [],
            "fee": None,
            "tax": None,
            "error_code": None,
            "error_description": None,
            "error_source": None,
            "error_step": None,
            "error_reason": None,
            "acquirer_data": {"bank_transaction_id": None},
            "created_at": 1790969354,
            "refunds": {"entity": "collection", "count": 0, "items": []},
        },
    },
}


def samples() -> None:
    for name, s in SAMPLES.items():
        save(
            name,
            {
                "unrecorded": True,
                "source": s["source"],
                "note": s["note"],
                "request": "GET /v1/payments/{id}?" + EXPAND,
                "http": 200,
                "body": redact(s["body"]),
            },
        )


def main() -> None:
    if sys.argv[1:] == ["live"]:
        live()
    elif sys.argv[1:] == ["samples"]:
        samples()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
