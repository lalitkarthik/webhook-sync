# Razorpay fetch answers (build step P2)

Recorded on **2026-10-02, 20:12-20:13 UTC**, from a Claude Code cloud session for [issue #20](https://github.com/lalitkarthik/webhook-sync/issues/20), by [`scripts/p2/record_fetch.py`](../../../../scripts/p2/record_fetch.py) (`live`, then `samples`). Every call went to `https://api.razorpay.com` in **test mode**: the script refuses a key that doesn't start `rzp_test_`, and it never prints or saves the key. **GET only**: nothing was created, captured or refunded. The payments are the ones the account already held (made on 2 October by P0 and the research run, [razorpay-fetch-facts.md](../../../../docs/research/razorpay-fetch-facts.md)).

Each file is one answer: `recorded_at`, `request` (method and path with query), `http` (status), `headers` (only `content-type`, `date`, `retry-after` and `uber-trace-id`, when present) and `body` (parsed JSON).

**Redacted in every file.** `email` is the placeholder `customer@example.com`; `contact` and `vpa` (also `upi.vpa`) are removed; card `last4` and `name` are removed; acquirer references (`rrn`, `arn`, `auth_code`, `bank_transaction_id`, `authentication_reference_number`, …) read `<redacted>`; an email in `notes` becomes the placeholder and a phone-like value in `notes` is removed. Everything else is as Razorpay sent it.

## Recorded (live, test mode)

The fetch is P2's own call, `GET /v1/payments/{id}?expand[]=refunds&expand[]=card`.

| File | What |
|---|---|
| `payments-list.json` | `GET /v1/payments?count=100`: the account's 7 test payments (4 captured, 1 failed, 2 created), used to find the ids below |
| `payment-captured-pay_Tj9rKBOXWcsMeE.json` | Captured, no refund; ₹500 netbanking, fee 1180, tax 180, `notes: []`, `description: null` (P0's manual-capture payment) |
| `payment-captured-pay_Tj66x2JuEXjIdB.json` | Captured, no refund; ₹500 via a payment link, fee 1298, tax 198, `notes` an object, `description` the link id |
| `payment-captured-pay_Tj64grZUMO3PEj.json` | Captured, no refund; ₹1, fee 2, tax 0, `notes` an object |
| `payment-captured-partly-refunded-pay_Tj9gmEfed6kRqO.json` | Captured, partly refunded: ₹500 with two processed ₹100 refunds in `refunds.items`, `amount_refunded` 20000, `refund_status: partial` |
| `payment-failed-pay_Tj9sm13R8h2378.json` | Failed at the mock bank; fee and tax null, `error_*` set |
| `payment-created-pay_Tj64GJlPGe5X07.json`, `payment-created-pay_Tj63Ohj3DwqGru.json` | Still `created` (Checkout opened, nothing completed) |
| `refunds-page-1.json`, `refunds-page-2.json`, `refunds-page-3-empty.json` | `GET /v1/payments/pay_Tj9gmEfed6kRqO/refunds?count=1` with `skip` 0, 1, 2: one refund, the other, then an empty page. **`count` is the number of items on the page, not the total**, and there is no `has_more` |
| `not-found-well-formed.json` | Fetch of `pay_00000000000000`: 400 `BAD_REQUEST_ERROR` "The id provided does not exist" |
| `not-found-malformed.json` | Fetch of `abc`: the same 400 |
| `refunds-unknown-payment.json` | `GET /v1/payments/pay_00000000000000/refunds?count=1`: **200 with an empty list**, not a 400 |

No authorised-but-uncaptured payment exists on the account, and every payment is netbanking in INR, so `card` is `null` on all of them.

## Unrecorded (Razorpay's documented samples)

Test mode can't produce these here (no card, UPI, wallet or international payment; no failed refund; no expired authorisation). Each file has `"unrecorded": true`, its `source` URL and a `note`, and its `body` is the documented sample, redacted the same way. Their `request` is the fetch they stand in for; they were not returned by it. Saved 2026-10-02 from the pages as they read that day.

| File | What | Source |
|---|---|---|
| `unrecorded-international-captured.json` | $1 USD payment with `base_amount` 7129 / `base_currency` INR, fee 207 (paise). The sample has no `captured` key | https://razorpay.com/docs/payments/international-payments/currency-conversion/ |
| `unrecorded-card-captured-expanded.json` | Captured Visa debit payment with an expanded `card` (`network`, `type`, `issuer`) | https://razorpay.com/docs/api/payments/fetch-with-id/ |
| `unrecorded-card-failed-expanded.json` | Failed RuPay credit payment with an expanded `card`; its `notes` held an email and a phone | https://razorpay.com/docs/api/payments/fetch-payment-expanded-card/ |
| `unrecorded-upi-captured.json` | Captured UPI payment: `email: null`, `upi` inline (`payer_account_type`, `flow`) | https://razorpay.com/docs/api/payments/fetch-with-id/ |
| `unrecorded-wallet-captured.json` | Captured wallet payment, `wallet: airtelmoney` | https://razorpay.com/docs/api/payments/fetch-with-id/ |
| `unrecorded-refund-failed.json` | **Put together**: the `refund.failed` webhook sample's payment entity, with its `failed` refund placed in `refunds` the way `expand[]=refunds` returns it | https://razorpay.com/docs/webhooks/refunds/ |
| `unrecorded-authorized-auto-refunded.json` | **Constructed, not a Razorpay sample**: an uncaptured payment after its auto-refund (`status: refunded`, `captured: false`). No page shows one (G66, spike S-Q7a); the page documents only that it happens. Replace it with a recording once S-Q7a runs | https://razorpay.com/docs/payments/payments/capture-settings/ |

## Re-recording

```bash
SSL_CERT_FILE=/root/.ccr/ca-bundle.crt uv run python scripts/p2/record_fetch.py live   # cloud session; drop SSL_CERT_FILE on a laptop
uv run python scripts/p2/record_fetch.py samples
```

`live` overwrites the files above and adds one per new payment. Check the redaction afterwards with the grep in issue #20.
