# Razorpay test-mode recordings (build step P0)

Recorded on 2026-10-03 (IST; 2026-10-02 UTC) on the owner's laptop, Windows 11, for [issue #4](https://github.com/lalitkarthik/webhook-sync/issues/4). The steps came from `docs/research/razorpay-first.md` Q2, Q4, Q5 and Q6. Everything ran in **test mode**: the key was checked as `rzp_test_` before every API call, and no key or secret is stored here.

**Setup.** [`scripts/p0/receiver.py`](../../../scripts/p0/receiver.py) listened on port 8080 behind `zrok2 share public localhost:8080 -n public:wsync-demo` (zrok v2.0.6, free account, no card). One test webhook pointed at `https://wsync-demo.shares.zrok.io/webhooks/razorpay` with 8 events. The receiver answered **500 to attempts 1-4 of every event id and 200 to attempt 5**, and 400 to an unsigned POST. [`scripts/p0/rzp.py`](../../../scripts/p0/rzp.py) made the orders, fetches, the capture and the refunds. The first payment was paid by hand in Checkout. The second and third were paid by a headless Chromium (Playwright) clicking the mock bank page's Success or Failure button.

## Files

| Path | What |
|---|---|
| `<event-id>-<attempt>.json` | One delivery each: arrival time, the headers as received, the raw body, its SHA-256, whether the signature verified, and what we answered. 11 events × 5 attempts = 55 files. |
| `receiver-log.jsonl` | One line per request, including two tunnel probes (the unsigned 400s). |
| `api/` | Every API response: orders, the payment while `authorized` and after capture, both refunds. |

**Edited files.** At Checkout the owner typed a real mobile number into the first payment. It was replaced by the dummy `+919123456780`, which has the same length, in the 35 deliveries that carry that payment: its `payment.authorized`, `payment.captured` and `order.paid`, and all four refund events. Each of those files has a `redacted` note. Their `body_sha256` and `signature` describe the bytes as received, so **those bodies no longer verify**. The other 20 deliveries are byte-exact and verify with the webhook secret: second payment `Tj9rs3245YOiVJ`, `Tj9sMcl7hE9PaT` and `Tj9sMvWj9V6X75`, and failed payment `Tj9sobwBnTgcRB`. All identity checks below ran on the bytes as received, before the edit.

## Retries (G38, G48, G62)

| Event | Event id | Gaps between attempts 1→2→3→4→5 (s) | 1→5 (s) | Event created → attempt 1 (s) |
|---|---|---|---|---|
| payment.authorized | Tj9h3likXEQFy4 | 1.2, 8.6, 9.8, 21.4 | 40.9 | 2.1 |
| payment.captured | Tj9h5SOW6XVf7g | 0.2, 7.7, 10.1, 21.3 | 39.4 | 1.6 |
| order.paid | Tj9h5yWwCUvsUn | 0.3, 7.5, 10.1, 21.3 | 39.2 | 1.8 |
| payment.authorized | Tj9rs3245YOiVJ | 0.8, 7.7, 10.2, 20.5 | 39.2 | 2.1 |
| payment.captured | Tj9sMcl7hE9PaT | 0.3, 8.1, 10.6, 20.4 | 39.4 | 1.6 |
| order.paid | Tj9sMvWj9V6X75 | 0.3, 7.8, 10.7, 20.5 | 39.2 | 1.8 |
| payment.failed | Tj9sobwBnTgcRB | 0.2, 6.9, 13.1, 20.8 | 41.1 | 1.2 |
| refund.created | Tj9tkWQjOGc6cJ | 0.2, 8.6, 13.6, 20.5 | 43.0 | 29.2 |
| refund.processed | Tj9tnpcnWxNGXY | 0.2, 5.6, 13.6, 20.5 | 40.0 | 32.2 |
| refund.created | Tj9tw9WT2xqs9D | 0.2, 8.2, 13.5, 21.0 | 42.9 | 28.8 |
| refund.processed | Tj9tzY1GfR6cQF | 0.4, 8.1, 10.5, 20.8 | 39.9 | 31.9 |

- **Intervals.** After a 500, the retry comes within about 1 s, then about 8 s, about 11 s and about 21 s later. **Five attempts span 39-43 s.** Attempts after the fifth weren't observed, because we answered 200 there. The docs' "exponential backoff for 24 hours" is consistent with this, but the later intervals are still unknown.
- **Same bytes.** On every attempt of every event, `x-razorpay-event-id`, `x-razorpay-signature` and the body SHA-256 were **identical**. Every signature verified against the webhook secret with HMAC-SHA256 of the raw body. A genuine retry is byte-identical to the first attempt (G38). The event id is present on every attempt and doesn't change (G62).
- **Headers.** Every delivery had `User-Agent: Razorpay-Webhook/v1`, `X-Razorpay-Event-Id`, `X-Razorpay-Signature` and `Request-Id`, plus the tunnel's `X-Forwarded-*` headers.
- **Refund events start late.** A refund event was created 4-5 s after its refund but reached us 29-32 s after the event's own `created_at`. Payment events took 1-2 s.
- **Dashboard log (Q2 step 4, G63, G60).** No webhook request log could be found in the current Dashboard. Accounts & Settings › Webhooks › the webhook shows only its settings, and the left menu has no Developers entry. So the check that Request Headers carry the signature, the hash of the copied body, the replay test (G63) and the secret rotation (G60) were **not done**.

## Tunnel (G49)

- Razorpay **accepted** `https://wsync-demo.shares.zrok.io/webhooks/razorpay` and saved it as Enabled.
- The reserved-name host is **`*.shares.zrok.io`**, not `*.share.zrok.io` as the research note and the issue say. `zrok2 overview` shows the right URL.
- **zrok's warning page appeared only for browsers.** Probes through the tunnel gave these results. A POST with a non-browser User-Agent reached the receiver (400 for unsigned, as designed). A POST or GET with a Chrome User-Agent got zrok's HTML interstitial with **status 200**, and the receiver never saw it. Razorpay's `Razorpay-Webhook/v1` was **not** caught: all 55 deliveries reached the receiver.
- zrok's console printed a "channel closed … connection was aborted" error during the session. No delivery was lost: every event has all five attempts.

## Payment states and fees (Q5, G59)

| Payment | Route | `authorized` | `captured` |
|---|---|---|---|
| `pay_Tj9gmEfed6kRqO`, ₹500 netbanking | auto-capture | webhook: fee/tax null | webhook: fee 1180, tax 180 |
| `pay_Tj9rKBOXWcsMeE`, ₹500 netbanking | order with `payment_capture: 0`, captured by API | `GET`: fee/tax **null**, `captured: false`; only `payment.authorized` arrived | `POST /capture` response already has fee 1180, tax 180; then `payment.captured` and `order.paid` |
| `pay_Tj9sm13R8h2378`, ₹500 netbanking | mock bank **Failure** | — | `payment.failed`, fee/tax null |

- **Fee and tax are null while `authorized` and set at capture.** The capture response itself carries them.
- **1180 = 1000 (2%) + 180 (18% GST).** That is the advertised 2% + GST. The research run on 2026-10-02 saw 1298/198 (2.2%) on the same amount and method. The test rate isn't stable, so test fees stay illustrative.
- **Manual capture needs no Dashboard change.** The Orders API still honours `payment_capture: 0` per order, though current docs don't list it. The account's capture setting was never changed.
- **Mobile number at Checkout.** Checkout rejected `9999999999` as "Please enter a valid mobile number". `9123456780` worked.
- **`account_id` is `acc_` + the Merchant ID.** The Dashboard shows Merchant ID `Tj5AnsI7BH2aLR`, and every delivery has `account_id: acc_Tj5AnsI7BH2aLR` (G59).

## Refunds (Q6)

Two partial refunds of ₹100 on `pay_Tj9gmEfed6kRqO`:

| Refund | Request | Create response | Reached `processed` | Webhooks |
|---|---|---|---|---|
| `rfnd_Tj9tB2hm5WLvww` | no `speed` | `pending`, `speed_requested: normal`, `speed_processed: normal` | within 25 s (first poll); its events were created 4 s after the refund, already `processed` | `refund.created`, `refund.processed` |
| `rfnd_Tj9tMhKlOulF3Q` | `speed: optimum` | `pending`, `speed_requested: optimum`, `speed_processed: normal` | within 16 s (first poll); events created 5 s after, already `processed` | `refund.created`, `refund.processed`; **no** `refund.speed_changed` |

- Test refunds go `pending` → `processed` in seconds. **Both `refund.created` and `refund.processed` carry `status: processed`**, so the event name doesn't tell the state; the entity does.
- `speed_requested` came back as `normal` even when no `speed` was sent. The docs say the field appears only when `speed` is set.
- `acquirer_data.arn` is already the dummy `10000000000000` in the first event.
- No `refund.failed` arrived, as Q6 expected.

## GIF decision (Q106's rule)

All five attempts arrive within about 40 s, which is well inside the 2-minute bound. **The duplicate scene films Razorpay's own retries in real time (option A).** The demo answers 500 to the first four attempts, and the caption reads: "Razorpay test mode. Our server answered 500 to the first four attempts on purpose; Razorpay retried the same event. One row; duplicates: 4." No speed-up or clock overlay is needed.

## Not done

S-Q3a (tie order of two payments in one second), G57 (`stripe sandbox create` from India), G60 (secret rotation mid-retry) and G63 (Dashboard replay). The last two need the Dashboard request log, which couldn't be found.
