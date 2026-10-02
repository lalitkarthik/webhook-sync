# Research: Razorpay as the first source (round 11, Q1-Q10)

Researched 2026-10-02 against primary sources and the real Razorpay test API, for the ten questions in [razorpay-first-brief.md](../design/razorpay-first-brief.md). New rows G45-G58 in [research-gaps.md](../design/research-gaps.md) point here; G21 and G18's open point were settled in the same session (see the Sheets section).

Primary means razorpay.com/docs (read as the Markdown copies Razorpay serves for each page), razorpay.com/pricing, the source of razorpay-python (commit 8960507), razorpay-cli (637de42) and razorpay-mcp-server, each tunnel's own docs and source (zrok at 0c9d346), stripe.com, docs.stripe.com, support.stripe.com and the source of stripe-cli (6a14b94), stripe-mock (98674a1), stripe/openapi and stripe-python (16.0.0). Calls were made to api.razorpay.com with the owner's **test** keys; the prefix was checked as `rzp_test_` before every run and no key value was printed or stored. A headless Chromium (Playwright 1.56.1) paid two test payments from a cloud session. Where only a secondary source exists, it is named and not counted. Four research passes ran in parallel; their API runs and test-mode writes are listed at the end.

| Q | Question | Verdict | One-line finding |
|---|---|---|---|
| Q1 | Test payments from a script | **contradicted, in the good direction** | An order by API, paid in headless Chromium through `checkout.js` and Razorpay's mock bank page (Netbanking, **Success**), is captured about 28 s after launch, with no human and no feature flag. Every server-to-server route needs Razorpay support to switch it on. |
| Q2 | Duplicate storm without a resend button | confirmed: no resend (corrected: a Dashboard replay now exists, razorpay-receiver-facts.md Q2) | No Dashboard or API resend; support replays one event at a time (≤ 15 days old). Razorpay retries any non-2xx for 24 h, but the intervals are not published. A recorded body verifies forever (HMAC of the body only). |
| Q3 | Outage scene by polling | **settled by a run** | `GET /v1/payments` lists newest first; `from` and `to` are both **inclusive**; `created` payments are listed; an undocumented `status` filter works. Unix seconds, 2000-2121, no maximum span. Tie order inside one second is unknown. |
| Q4 | The tunnel | recommendation | Razorpay refuses 11 testing hosts (including `ngrok.io`, `loca.lt`) plus local names; ports 80/443 only; it suggests zrok. zrok's free-account interstitial answers **200**, which could swallow a delivery. Fallback: Cloudflare Quick Tunnel. |
| Q5 | Fee and tax in test mode | confirmed | Null while `created`; filled at capture (1298/198 paise on ₹500 netbanking). The test rate (2.2% + GST) differs from the advertised 2%, so test fees are illustrative. |
| Q6 | Refunds and disputes in test mode | settled: neither failure can be produced | A refund can be made; a failed refund and a dispute can't be produced by any documented means. Those paths are tested on documented sample payloads only. |
| Q7 | The sweeper with no events list | **settled, with a design gap** | A `failed` payment can turn `authorized` up to 3 days later; refunds can stay `pending` about 14 days; no endpoint lists by update time. The hourly re-fetch of `authorized` payments misses changes; an hourly **re-list** by creation window catches them. |
| Q8 | Mode | confirmed | No mode in payloads, headers or API responses. The key prefix is the only hard check. Webhook secrets differ between modes only if the owner types different ones. `GET /v1/webhooks` works with a merchant key. |
| Q9 | Stripe | confirmed | India is still invite-only; a test-only account under another country would misstate it. `stripe sandbox create` (CLI ≥ 1.42) gives a 7-day sandbox without an account, untried from India. Five open Stripe spikes, not eight. |
| Q10 | Build order | internal | No outside fact; settled in round 11 from the above. |

## Q1: making test payments from a script

**Question.** How far can `wsync demo` create *and complete* Razorpay test-mode payments without a person at a browser?

**Verdict: fully automatic, with a headless browser.** The brief says "a test payment is a Payment Link made by API and paid by hand". That is too pessimistic. Both a plain order and a Payment Link were paid here by a script, with no human input. No API route can complete a payment without a browser on an account with default features.

**What the sources say.**

*Mock bank pages.* The test-card page says "Test mode features a mock bank page with Success and Failure buttons to replicate the live payment experience." The Standard Checkout integration page repeats this. It adds that for netbanking "Razorpay will redirect to a mock page where you can make the payment `success` or a `failure`", and that for wallets it shows the same mock page. For cards, the test-card page says a sample page asks for an OTP: "Enter a random OTP between 4 to 10 digits to make the payment successful", or fewer than four digits to fail it. So no step needs a real person: every OTP and bank login is simulated. The page lists six Indian success cards (for example Visa 4100 2800 0000 1007) and error cards by failure reason. For error cards, "you must select failure to get the right error".

*Test UPI IDs.* The test-UPI page says to enter `success@razorpay` or `failure@razorpay` at Checkout. It warns: "In test mode, payment cancellation will result in a successful payment." But the UPI method page says the UPI Collect flow (typing a VPA) "is being deprecated effective 28 February 2026". iOS and mobile web are among the exemptions. The S2S test page adds: "use **Test Mode** to test UPI payments, and **Live Mode** for UPI Intent and QR payments." In the headless desktop Checkout run here, the payment options were only Cards, Netbanking and Wallet. UPI wasn't offered, so `success@razorpay` could not be used.

*(a) Payment Links.* The Payment Links "create" page and the "Create a Standard Payment Link" API page both carry the same box: "Test Mode Limit. In test mode, you can create up to 30 Payment Links per business. If you need to create more than 30 Payment Links for testing purposes, contact Razorpay Support." No page, FAQ included, says whether cancelled or expired links count. Payment Links have a cancel call, but no delete. The documented test steps are by hand: open the link in a browser, pay, "Select Success or Failure". The API's error list adds: "UPI Payment Links is not supported in Test Mode."

Own run: one link for ₹500 was created by API with notifications off. Headless Chromium opened its short URL, which lands on `razorpay.com/payment-link/plink_…/test`. The script typed a dummy mobile number, chose Netbanking and a bank, and clicked Success. The link became `paid`, and its payment was `captured`. Razorpay made an order behind the link (`order_Tj65caO9GByARz`), so a link payment also produces `order.paid` and `payment_link.paid` events.

*(b) A local Standard Checkout page.* The page loads `https://checkout.razorpay.com/v1/checkout.js` and opens it with the test `key_id` and an `order_id` from `POST /v1/orders`. Own run: the page was served to Chromium by request interception under a made-up https origin, so it needed no web server and no registered website. The script clicked Netbanking and then a bank. That opened a popup at `api.razorpay.com/v1/gateway/mocksharp/payment` reading "This is just a demo bank page… Success Failure". The script clicked Success. Checkout's `handler` then received `razorpay_payment_id`, `razorpay_order_id` and `razorpay_signature`. The payment was `captured` within about 20 s, under the default auto-capture setting. It took 27.8 s from launching the browser to the handler, including fixed 6 s waits. Two earlier probes opened the mock page and then closed the browser. Each left a payment stuck in `created` on the order, and both appear in `GET /v1/payments`.

Checkout loads third-party fraud and captcha frames (sardine.ai, hCaptcha, Stripe's `m.stripe.network`). None of them challenged the headless browser here. That could change without notice.

*(c) Server-to-server APIs.* The S2S JSON V1 and JSON V2 pages each say: "This is an on-demand feature. Please raise a request with our Support team to get this feature activated on your Razorpay account." The S2S Redirect page lists "Raise a request with our Support team to get this feature enabled" as a prerequisite. The UPI Intent S2S page says the same, and the S2S overview requires PCI-DSS certification for card data. The Recurring Payments API is "an on-demand feature" too. The SDKs all wrap `payments/create/json`, `payments/create/upi` and `payments/create/recurring` (for example razorpay-python's `createPaymentJson`, `createUpi` and `createRecurring`). None of them has a test-payment or simulate call.

Own run: `POST /v1/payments/create/json` with `method: upi`, `flow: collect` and `vpa: success@razorpay` returned HTTP 400 `BAD_REQUEST_ERROR`, "The requested URL was not found on the server." The order's `attempts` stayed 0. The account answers the same way for `GET /v1/payments/qr_codes` and `GET /v1/virtual_accounts`. That is how Razorpay reports a feature that isn't enabled on the account.

*(d) Other routes.*
- **Capture.** `POST /v1/payments/{id}/capture` only moves an `authorized` payment to `captured`. It can't create one. The capture-settings page says auto-capture "is the default setting for all customers". It also says capture settings apply only to payments created through the Orders API.
- **BharatQR.** Its testing page documents a fully API-driven test payment, `POST /v1/bharatqr/pay/test` with `reference` (a `qr_…` id), `amount` and `method` (`card` or `upi`). It "will trigger the same webhook notifications that a live payment made via BharatQR code would." But BharatQR needs a support ticket to activate. QR Codes also need a request to enable, and this account returns "not found" for QR codes. The QR pages add: "You can scan QR Codes that are created only in **Live Mode**."
- **Smart Collect.** The page "Make Test Payments" covers only the Dashboard: Smart Collect › a Customer Identifier › **Make a Test Payment** (NEFT, RTGS or IMPS). It names no API. Its prerequisites say: "Ensure that you do not fall under the `Individuals` merchant category as Smart Collect is not available for this Merchant Category Code". The owner is an individual.
- **Subscriptions.** After one authentication payment, "In test mode, you can simulate these charges from the Dashboard using the Charge this now button". That button is on the Dashboard only, with no API. Test card tokens last 3 days.

*(e) KYC.* The "Test and Live Modes" page: "The Test mode is available to you as soon as you complete the sign-up process. You can use Test mode as long as you want." Live mode "is available after you activate your account".

**Ranked routes, most to least automatic.**

| # | Route | What still needs a human | Status |
|---|---|---|---|
| 1 | `POST /v1/orders`, then a local `checkout.js` page in headless Chromium; Netbanking, any bank, **Success** on the mock page | Nothing. Needs Playwright and Chromium. No feature flag, and no test-mode cap on orders is documented. | Done here (`pay_Tj64grZUMO3PEj`) |
| 2 | `POST /v1/payment_links`, then headless Chromium on the short URL; dummy mobile number, Netbanking, Success | Nothing, but each payment uses one of 30 links per business | Done here (`pay_Tj66x2JuEXjIdB`) |
| 3 | Route 1 with a test card and the sample OTP page (any OTP of 4-10 digits) | Nothing, as documented | Not tried (no second order allowed) |
| 4 | `POST /v1/bharatqr/pay/test` | A support ticket to activate BharatQR and QR Codes | Documented; not enabled on this account |
| 5 | S2S JSON V1/V2, S2S Redirect, UPI Intent S2S, recurring | A support ticket per feature; PCI-DSS for card data; UPI Collect deprecated; Intent works only in live mode | Tried `create/json`: "URL not found" |
| 6 | Subscriptions, "Charge this now" | A click on the Dashboard for every charge, after one checkout payment | Documented |
| 7 | Smart Collect "Make a Test Payment" | A click on the Dashboard; activation; not for individuals | Documented; not enabled |
| 8 | A Payment Link paid by hand in a browser | All of it | Documented fallback |

**What the design should change**
- [razorpay-first-brief.md](../design/razorpay-first-brief.md): replace "There is no equivalent of `stripe trigger`. A test payment is a Payment Link made by API and paid by hand" with route 1. Also replace "paying test Payment Links in a browser" in the list of laptop-only work: a cloud session paid both payments here. Only receiving webhooks stays on the laptop.
- [showcase.md](../design/showcase.md) "wsync demo": `duplicates` and `outage` create payments by "order by API, paid headless through Checkout's mock bank page", not the Stripe CLI. Print the Checkout or link URL as a by-hand fallback if the headless run fails. Use netbanking amounts of ₹100 or more so the GST column isn't 0 (see Q5). Budget about 30 s per payment: ten payments for the outage scene take about 5 minutes, or less if they run in parallel.
- [testing.md](../design/testing.md): the weekly real-services run can make real Razorpay test payments headless, in CI as well. Add to the recording spike: two abandoned attempts that stay `created`, and one link payment with its extra `order.paid` and `payment_link.paid` events.
- [build-order.md](../design/build-order.md): Playwright is a demo-only dependency. Put it behind an optional extra (for example `wsync[demo]`), so the service itself never needs a browser.
- [sources.md](../design/sources.md): `GET /v1/payments` returns abandoned attempts in `created`, two of them from these runs. The existing rule "a payment still `created` makes no row" is needed. The hourly re-fetch of `authorized` payments should also cover them, in case they move on.
- [research-gaps.md](../design/research-gaps.md): close the brief's "not yet tried" bullets on KYC and the 30-link cap. Add a row for whether cancelled links count.

**Open point / Spike**
- On the owner's laptop, with a webhook through the tunnel: run route 1 once and record `payment.authorized`, `payment.captured` and `order.paid`. Run route 2 once and also record `payment_link.paid`. This also serves Q2 and Q5.
- Route 3 (card plus sample OTP page) is documented but untried. Netbanking is enough for the demo. Try a card only if the GIF should show method `card`.
- Checkout loads fraud and captcha scripts. If a later run gets a challenge, `wsync demo` falls back to printing the URL for a person to pay.
- Ask Razorpay support whether cancelled or expired test links count toward the 30 only if route 2 becomes the main route. Route 1 makes this moot.
- Re-read the two `created` attempts (`pay_Tj63Ohj3DwqGru`, `pay_Tj64GJlPGe5X07`) after a day to see whether Razorpay moves them to `failed`.

URLs: https://razorpay.com/docs/payments/payments/test-card-details/, https://razorpay.com/docs/payments/payments/test-upi-details/, https://razorpay.com/docs/payments/payment-gateway/web-integration/standard/integration-steps/, https://razorpay.com/docs/payments/payment-links/create/, https://razorpay.com/docs/payments/payment-links/faqs/, https://razorpay.com/docs/api/payments/payment-links/create-standard/, https://razorpay.com/docs/payments/payment-gateway/s2s-integration/, https://razorpay.com/docs/payments/payment-gateway/s2s-integration/json/v1/, https://razorpay.com/docs/payments/payment-gateway/s2s-integration/json/v2/, https://razorpay.com/docs/payments/payment-gateway/s2s-integration/json/v2/test-integration/, https://razorpay.com/docs/payments/payment-gateway/s2s-integration/json/v2/build-integration/upi/collect/, https://razorpay.com/docs/payments/payment-gateway/s2s-integration/redirect/, https://razorpay.com/docs/payments/payment-gateway/s2s-integration/payment-methods/upi/intent/, https://razorpay.com/docs/payments/payment-methods/upi/, https://razorpay.com/docs/api/payments/recurring-payments/, https://razorpay.com/docs/api/payments/capture/, https://razorpay.com/docs/payments/payments/capture-settings/, https://razorpay.com/docs/payments/payment-methods/bharatqr/testing/, https://razorpay.com/docs/payments/qr-codes/, https://razorpay.com/docs/payments/qr-codes/faqs/, https://razorpay.com/docs/payments/smart-collect/, https://razorpay.com/docs/payments/smart-collect/test-payments/, https://razorpay.com/docs/payments/subscriptions/test/, https://razorpay.com/docs/payments/dashboard/test-live-modes/, https://github.com/razorpay/razorpay-python/blob/master/razorpay/resources/payment.py

## Q2: the duplicate-storm scene without a resend button

**Question.** The GIF promises "the same delivery sent five times ends as one row". Razorpay has nothing like `stripe events resend`. What can produce five deliveries, and what must the caption say for each way?

**Correction (2026-10-02):** a self-service Dashboard replay now exists, for unacknowledged events up to 7 days old, 5 a day; see [razorpay-receiver-facts.md](razorpay-receiver-facts.md) Q2.

**Verdict:** There's no self-service resend, by Dashboard or API. Two honest options remain. (A) Provoke Razorpay's own retries: a demo-only switch stores the event, then answers 500 to the first four attempts. (B) Replay one recorded, correctly signed delivery four times ourselves. A is the stronger proof but its timing is unknown, so the spike decides. B always works and needs an exact caption.

**What the sources say.**

*a. Resending by hand.* The FAQ "Where can I find the webhooks log history?" points to Dashboard › Developers › Webhooks, then a webhook, then its log (webhooks/faqs). The two screenshots on that FAQ show a test-mode page titled "Webhook Request Logs in Past 7 Days". It lists Event ID, Event Type and date, with a search box and a Response Code filter. Its "Webhook details" panel shows event type, URL, status (200) and four sections, Request Headers, Request, Response Headers and Response, each with a copy icon. **No retry or resend control is visible.** The screenshots date from September 2024, so the current Dashboard may differ. The only replay path is the FAQ "Can a webhook event be replayed from Razorpay's side?". It is a support request via Help › Have a query? › Technical Support › Issue regarding Webhooks/API. It needs these conditions: "The webhook should be enabled on the dashboard during the occurrence of this event", "The webhook event should not be older than 15 days", and "Currently, bulk replaying of webhook events is not possible." No API resends. The webhooks API pages are under the partners section and cover only create, fetch, fetch all, update and delete (api/partners/webhooks). The Razorpay CLI's commands are configure, customers, disputes, documents, invoices, orders, payment-links, payments, qr-codes, refunds, route, settlements, smart-collect and subscriptions. None of them is about webhooks (razorpay-cli `cmd/`). Its install page does say it can "trigger events", but that means calling the APIs.

*b. Razorpay's own retries.* "Every event that receives a non-2xx response is considered an event delivery failure … we retry the delivery in exponential backoff policy for 24 hours after event creation timestamp" (webhooks/best-practices). A reply slower than 5 s also fails: "your server accepts the event but fails to respond in 5 seconds … the session is marked timeout. It is assumed that the webhook was not processed and is sent again" (same page; setup-edit-payments says "a status code in the range `2XX` within a window of 5 seconds"). After 24 h of failures "the webhook is disabled. You need to enable the webhook from the Dashboard after fixing the errors". So nothing re-enables it automatically. An email goes to the webhook's Alert Email, or failing that to the account's email (best-practices). The setup page says the alert address gets "webhook related notifications like failures, deactivation and so on" (setup-edit-payments). **No page gives the intervals, the number of attempts or the delay before the first retry.** Searches found no official schedule either. Whether retries carry the same bytes is still G38. Two pages imply the same signed payload, but neither says so. One says to "use the old secret for webhook signature validation while retrying older requests" (validate-test, faqs). The other lists, among the replay conditions, that "signature validation should be done using the same signature during the occurrence of this event" (faqs). The id is described as "unique per event", which reads as the same `x-razorpay-event-id` on every retry (best-practices).

*c. Replaying ourselves.* The signature is "HMAC with SHA256 algorithm; with your webhook secret set as the key and the webhook request body as the message". The page's pseudo-code has only `message = webhook_body` (validate-test). razorpay-python's `verify_webhook_signature` calls `verify_signature`, which computes `hmac.new(key=key, msg=body, digestmod=hashlib.sha256)` and compares digests. No timestamp or tolerance is involved (`razorpay/utility/utility.py`). So a recorded body with its `x-razorpay-signature` verifies for as long as the secret is unchanged. Our receiver already plans for this: the event id is outside the signature, so it also dedupes on the signed body's hash (receiver.md, Q86). A replay of one real delivery therefore runs exactly the receiver path a byte-identical retry would run. It does not show Razorpay sending anything. If G38 finds that retries are **not** byte-identical, a replay is no longer a stand-in for a retry. It still shows replay protection.

*d. A test tool.* The validate-test page names three ways to test: request-interceptor sites, localhost (via a tunnel) and staging. All of them rely on real "Test events [that] get triggered on a transaction done in the **Test** mode". No page documents a "send test event" button. The payload samples on webhooks/payments are illustrations with no signature. razorpay-python's `tests/mocks` are unsigned JSON (G41). A test-mode event is signed with the secret the owner typed for that webhook. That secret "does not need to be the Razorpay API key secret" (setup-edit-payments).

**Captions the GIF needs.**
- Option A, provoked retries: "Razorpay test mode. Our server answered 500 to the first four attempts on purpose; Razorpay retried the same event. One row; duplicates: 4." If the footage is sped up, add "(N min, sped up ×K)" with a visible clock.
- Option B, replay: "One real Razorpay test delivery, recorded, then re-sent four more times by us, byte for byte. Razorpay signs only the body, so each copy still verifies. One row; duplicates: 4." It must not say or imply that Razorpay sent five.
- A support-ticket replay is one event at a time, on Razorpay's schedule, and can't be filmed on demand. It is not an option.

**What the design should change**
- [showcase.md](../design/showcase.md) GIF scene 1 and the `wsync demo duplicates` row: drop `stripe events resend`. Describe option A if the spike shows retries arrive within about two minutes in total, else option B, with the caption above. Put the caption on screen, not only in the README.
- [receiver.md](../design/receiver.md): add a demo-only fault switch, for example `--demo-fail-first 4`. It stores the event and its job as normal, then answers 500 to the first N attempts per event id. This is how a real "accepted but the answer was lost" looks. Startup refuses it unless the key is `rzp_test_` and the spreadsheet is on the demo allow-list. While on, it logs each attempt's arrival time, `x-razorpay-event-id` and body SHA-256, which is also the G38 instrument.
- [testing.md](../design/testing.md) "load: duplicate storm": drive it from a recorded, correctly signed fixture replayed five times. Add one case where the replay carries a **different** `x-razorpay-event-id` and is still caught as a duplicate by the body hash.
- [edge-cases.md](../design/edge-cases.md) H2: the alert text should say a disabled Razorpay webhook stays disabled until someone re-enables it under Accounts & Settings › Webhooks.
- [research-gaps.md](../design/research-gaps.md): new rows for "Razorpay retry intervals and first-retry delay" (open, spike) and "no Dashboard or API resend" (closed). G38 stays open.
- README honest limits: "Razorpay has no resend button; the duplicate scene uses [A or B]."

**Open point / Spike** (owner's laptop, test mode; one run settles G38 and the timing):
1. Start the receiver with the fault switch at N = 4 behind the tunnel chosen in Q4. Create a test-mode webhook for `payment.captured` to `https://<tunnel>/webhooks/razorpay` with a fresh secret (OTP `754081` in test mode).
2. Pay one test Payment Link (Q1). For each attempt, log the arrival time, `x-razorpay-event-id`, `x-razorpay-signature`, the body SHA-256 and the `User-Agent`.
3. Record: the gaps between attempts 1-5; whether the event id, signature and body hash are identical; and whether the Dashboard log shows one row per attempt.
4. In the Dashboard log, open the delivery and check whether Request Headers include `x-razorpay-signature`. Then check whether the copied Request equals the raw body byte for byte (hash it). If both hold, a delivery can be recorded from the Dashboard alone.
5. Decision rule: attempts 1-5 inside about 2 minutes means film A in real time. Up to about 30 minutes means A, sped up with a clock. Longer means B.

URLs: https://razorpay.com/docs/webhooks/faqs/, https://razorpay.com/docs/webhooks/best-practices/, https://razorpay.com/docs/webhooks/setup-edit-payments/, https://razorpay.com/docs/webhooks/validate-test/, https://razorpay.com/docs/webhooks/payments/, https://razorpay.com/docs/build/browser/assets/images/webhook-logs.jpg, https://razorpay.com/docs/build/browser/assets/images/webhook-log-details.jpg, https://razorpay.com/docs/api/partners/webhooks/, https://razorpay.com/docs/api/install-cli/, https://github.com/razorpay/razorpay-cli/tree/master/cmd, https://github.com/razorpay/razorpay-python/blob/master/razorpay/utility/utility.py

## Q3: the outage scene by polling

**Question.** Payments made while the server is down must appear after restart through the sweeper's poll of `GET /v1/payments`. What is the list's ordering? Are `from` and `to` inclusive? What is the unit, and is there a maximum window? Can `skip` paging miss or duplicate items? Are `created` payments listed? The same for `GET /v1/refunds`.

**Verdict: settled by a run, except tie order.** The docs settle the unit and limits but not ordering, inclusivity or whether `created` payments are listed. The first pass ran on an empty account; once Q1's runs had made four test payments, a follow-up run on 2026-10-02 settled all three (see *Follow-up run* below). How payments created in the same second are ordered is still unknown, and the paging rule in (d) doesn't depend on it.

**What the sources say.**

*(a) Ordering.* "Fetch All Payments" says "By default, only the last 10 records are displayed", and "Fetch All Refunds" says "by default, only the last 10 refunds are returned". Neither says how items are sorted. Every sample is newest first: the payments sample lists `created_at` 1667397881, then 1667397781. The refunds sample lists 1594982363, then 1589521675. The disputes sample, and razorpay-python's `tests/mocks/dispute_collection.json`, list 1590059211, then 1590058554. razorpay-python's `payment_collection.json` and `refund_collection.json` mocks give both items the same `created_at`, so they show nothing about ties. No page says how items created in the same second are ordered. No page offers a sort parameter. Our runs found none either: `sort`-style names weren't tried, but every unknown name on `/v1/payments` is rejected (see (c)).

*(b) Inclusivity.* The "Fetch All Payments" page says `from` is the "UNIX timestamp, in seconds, from when payments are to be fetched" and `to` is the one "till when payments are to be fetched". The refunds page says "at which the refunds were created" and "till which the refunds were created". The general "Understand Razorpay APIs" page, under Pagination, says `from` is the "Timestamp, in Unix, after which the entities are created" and `to` is the one "before which the entities are created". That wording leans towards exclusive, but it is a general prose description and contradicts "from when" on the endpoint page. No page states inclusivity. The razorpay-mcp-server source (`pkg/razorpay/payments.go`) only repeats the doc wording. Our run with `from = to = 1700000000` returned HTTP 200 and an empty list. That shows equal bounds are allowed, but with no payments it says nothing about inclusivity.

*(c) Unit and window.* Unix **seconds**, by both pages and by the API. `from=1759000000000` (milliseconds) on `/v1/payments` returned 400 "from must be between 946684800 and 4765046400". The same message is on the docs page. 946684800 is 2000-01-01 and 4765046400 is 2121-01-01. `to=4765046401` was rejected the same way. There is **no maximum span**: `from=946684800&to=4102444800` (2000 to 2100) returned 200. `from > to` returned 200 and an empty list, not an error. `count` is 1 to 100 (0 and 101 are rejected), and `skip` must be ≥ 0. **`GET /v1/refunds` doesn't range-check `from`**: a millisecond `from` returned 200 and an empty list. It also ignores unknown parameters (`foo=1` returned 200), while `/v1/payments` rejects them ("foo is/are not required and should not be sent"). A unit bug on refunds would therefore fail silently.

*(d) `skip` paging during writes.* No page discusses consistency of `skip` paging. Reasoning from the facts above, assuming newest first:
- In a window whose `to` is already in the past (the sweeper's slice ends at `source_now − 60 s`), new payments have `created_at > to`. They can't enter the window, so they can't shift items between pages. A payment's `created_at` is fixed at creation; later changes (authorised, captured, refunded) don't move it, because `created_at` is "Timestamp, in UNIX format, on which the payment was created" (payments entity page).
- If `to` is open or in the future, a payment created during paging enters at the top. With newest-first order it pushes every item down by one, so the next `skip` page **repeats** one item. It doesn't miss one. Dedupe by id makes repeats free.
- The one undocumented risk is ties. If several payments share a second and the server's order among them isn't stable between calls, `skip` could repeat one and miss another at a page boundary.
- Safe paging that doesn't depend on inclusivity or tie order: page with `count=100` and no `skip`. For the next page set `to = oldest created_at seen + 1` and dedupe by id. Under an inclusive `to` this re-reads the boundary seconds. Under an exclusive `to` it still includes the oldest second. Stop when a page has fewer than 100 new ids. If a page makes no progress (more than 100 payments in two seconds), fall back to `skip` paging inside that two-second window.

*(e) `created` payments.* The lifecycle page says `created` "is the first state. The customer has provided the payment details, which are sent to Razorpay. The payment has not been processed yet." The late-authorisation page says a payment with no bank response "remains in the **Created** state for the first 10 minutes and is later marked as **Failed**". No page says whether `created` payments appear in `GET /v1/payments`. Our run found that `/v1/payments` accepts an **undocumented `status` filter**. It is validated: `status=bogus` returns 400 "Not a valid status: bogus", while `created`, `authorized`, `captured`, `refunded`, `failed` and `pending` all return 200. A validated `status=created` suggests `created` payments are listable, but that is an inference, not a source. Whether the filter actually filters couldn't be tested on an empty account.

*(f) Refunds.* The refunds page has the same four parameters and the same "last 10" default. A refund has no `created` state. Its statuses are `pending`, `processed` and `failed` (refunds entity page), so every refund is listable once it exists. `/v1/refunds` also accepts an undocumented, validated `status` filter: `pending`, `processed` and `failed` return 200, and `created` returns 400 "The status must be a valid value."

*Follow-up run (2026-10-02, after Q1's payments existed).* Four test payments, created at 1790956537, 1790956586, 1790956611 and 1790956739 (two `created`, two `captured`):
- `GET /v1/payments?count=100` returned all four in strictly descending `created_at`: **newest first**. `count=1` with `skip` 0-3 walked them in the same order.
- For `pay_Tj64grZUMO3PEj` at T = 1790956611: `from=T&to=T`, `from=T-1&to=T` and `from=T&to=T+1` each returned it; `from=T+1&to=T+100` didn't, and `from=T-100&to=T-1` returned only the two older payments. So **`from` and `to` are both inclusive**.
- The two abandoned attempts in `created` were listed, so **`created` payments appear in the list**.
- `status=captured` returned only the two captured payments, so the undocumented **`status` filter filters**.

**What the design should change** (inclusivity is now known, but the rule below still holds and keeps one code path for every source)
- [sweeper.md](../design/sweeper.md) "Razorpay: polling" and [sources.md](../design/sources.md) "Sweep and polling": state that Razorpay documents neither order nor inclusivity, though the follow-up run shows newest first and both ends inclusive, and give the paging rule from (d): shrink `to` to the oldest `created_at` seen plus one, dedupe by id, and fall back to `skip` only inside a stuck two-second window. Request each slice as `from = start − overlap`, `to = end`, and keep only items with `created_at < end` on our side. Then inclusivity doesn't matter, because the next slice's overlap re-reads the boundary.
- The adapter asserts `from ≤ to` and that both are seconds within 946684800-4765046400 **before** calling. Razorpay answers `from > to` with an empty list, and refunds accept milliseconds silently.
- [sources.md](../design/sources.md) "Fetch": a listed `created` payment makes no row (G41 already says so). The re-list in Q7 picks it up once it moves on.
- [testing.md](../design/testing.md): the fake Razorpay list server should return items newest first, with **ties in one second in an unstable order**, and admit a new payment between page calls. That tests the paging rule against the worst case the docs allow.

**Open point / Spike**
- S-Q3a (laptop, optional): make two payments in the same second and repeat the list five times to see tie order. The paging rule in (d) is safe either way.

URLs: https://razorpay.com/docs/api/payments/fetch-all-payments/, https://razorpay.com/docs/api/refunds/fetch-all/, https://razorpay.com/docs/api/disputes/fetch-all/, https://razorpay.com/docs/api/understand/ (Pagination), https://razorpay.com/docs/api/payments/entity/, https://razorpay.com/docs/payments/payments/late-authorisation/, https://github.com/razorpay/razorpay-python/tree/master/tests/mocks, https://github.com/razorpay/razorpay-mcp-server/blob/main/pkg/razorpay/payments.go

## Q4: the tunnel

**Question.** Which public HTTPS tunnel will Razorpay accept for real deliveries to the owner's laptop, and which should the owner use?

**Verdict:** Razorpay refuses a named list, and the list doesn't include any of the candidates below. It also says "many common tunneling services are blacklisted", so a host missing from the list isn't proof it will be accepted. **Recommendation: zrok with a reserved name**, because Razorpay names it. But first prove that zrok's free-account interstitial page doesn't intercept Razorpay's POST. **Fallback: a Cloudflare Quick Tunnel.** It needs no account and has no interstitial, but its URL changes every run, so the webhook URL has to be edited each session. Tailscale Funnel is the option with a stable URL if zrok fails.

**What the sources say.**

*Razorpay's rules.* The validate-test page's "Blacklisted Domains" lists two groups. Third-party testing services: `burpcollaborator.net`, `oast.pro`, `interact.sh`, `canarytokens.com`, `requestbin.com`, `webhook.site`, `hookbin.com`, `beeceptor.com`, `mockbin.org`, `ngrok.io`, `loca.lt`. Internal and local domains: `metadata.google.internal`, `localhost`, `localhost.localdomain`, `.onion`, `.local`, `.internal`, `.corp`. `ngrok-free.app`, `ngrok-free.dev`, `ngrok.app`, `ngrok.dev`, `trycloudflare.com`, `share.zrok.io`, `ts.net`, `localhost.run`/`lhr.life`, pinggy and serveo are **not** listed. The same page says: "Due to security restrictions, many common tunneling services are blacklisted. You can handle this by creating a tunnel to your localhost using `zrok`." Other rules:
- "Webhook URLs must use ports **80** or **443** only" (webhooks overview).
- "We recommend using an HTTPS URL", so HTTP on port 80 isn't refused by the docs. Production refuses TLS 1.0 and 1.1 (setup-edit-payments; best-practices).
- "If your URL contains `razorpay` as a domain, you will not be able to add the URL" (setup-edit-payments).
- Saving a localhost or private address gives the error "private ip found for host" (faqs), which suggests the host is resolved and checked. A literal public IP isn't covered either way.
- Each mode has its own webhooks, "separate URLs for **Live** mode and **Test** mode", and an account can have up to 30 payment webhook URLs (webhooks overview; faqs).
- Webhooks come from fixed egress IPs, listed at /docs/security/whitelists.

*zrok* (now v2, command `zrok2`). An account is needed: "Go to myzrok.io and sign up for a free account", then copy the account token (zrok `get-started/get-token.md`). Then `zrok2 enable <your_account_token>` and `zrok2 status` (`enable-env.md`). `zrok2 share public 9090` gives a public URL that lasts while the command runs, because "zrok shares are ephemeral by default" (`create-share.md`). For a stable URL: `zrok2 create name -n public myapp`, then `zrok2 share public localhost:8080 -n public:myapp`. The name "persists even when your share isn't running" (`how-tos/shares/manage-reserved-names.md`), and the URL has the shape `https://myapp.share.zrok.io` (`migrate-v1-to-v2.md`). Free plan: "$0 per month", 5 GB a day, 25 environments, "50 Share Backends (Ephemeral or Reserved)" (zrok.io/pricing). Rate limits are 2,000 requests per 300 s per IP (`myzrok/service-limits.md`). **The catch:** the pricing page says an interstitial page is shown "the first time someone visits a public share created by an unverified free account", and to "Add a credit card anytime to remove interstitial pages". In zrok's proxy source, the interstitial is written with `w.WriteHeader(http.StatusOK)` and an HTML body (`endpoints/proxyUi/interstitial.go`). It is skipped only for `OPTIONS`, a `skip_zrok_interstitial` header, a `zrok_interstitial` cookie, or a User-Agent that doesn't match the frontend's `user_agent_prefixes` (`endpoints/publicProxy/http.go`). With no prefix list, "all user agents will receive an interstitial page" (`etc/frontend.yml`; CHANGELOG v2.0.3). The hosted zrok.io frontend's prefix list isn't published. If it catches Razorpay's User-Agent, Razorpay receives 200 and never retries, and the event silently never reaches us. The sweeper would bring the payment back later, but the scene would be wrong. Windows install: download the Windows archive, then run the PowerShell block in `how-tos/install/windows.mdx`, which extracts `zrok2.exe` to `%USERPROFILE%\bin` and adds it to PATH, then `zrok2 version`.

*Cloudflare Quick Tunnel.* "You do not need a Cloudflare account or domain"; the command is `cloudflared tunnel --url http://localhost:8080`. "The hostname changes each time you create a Quick Tunnel"; it has "up to 200 in-flight requests" and "no uptime guarantee"; it is "for testing and development" (developers.cloudflare.com trycloudflare). No interstitial is documented. On Windows, Cloudflare's downloads page offers the .exe or .msi from GitHub releases (no winget command is given). Its macOS command is `brew install cloudflared`.

*localhost.run.* `ssh -R 80:localhost:8080 localhost.run`; "no account setup is needed for free domains" (localhost.run/docs). On the free plan "Domain names change regularly" and there is "a speed limit". A permanent name needs sign-up at admin.localhost.run with an SSH key (localhost.run/docs/forever-free). No Razorpay doc mentions it.

*Tailscale Funnel.* It is "available for all plans", including the free Personal plan. URLs are `https://<device>.<tailnet>.ts.net`, stable per device. "Funnel can only listen on ports `443`, `8443`, and `10000`", which suits Razorpay's 443 rule. Certificates are automatic. Traffic "is subject to non-configurable bandwidth limits" (tailscale.com/kb/1223). Commands: `tailscale funnel 3000`, or `tailscale funnel --bg 3000` to keep sharing across restarts; `tailscale funnel reset` turns it off (tailscale.com/kb/1311). It needs a Tailscale account and the client installed.

*pinggy.* `ssh -p 443 -R0:localhost:8000 free.pinggy.io`, "no signup"; free URLs look like `<random>.run.pinggy-free.link` (pinggy.io/docs). The free plan has a "60 minutes tunnel timeout" and "Random subdomains" (pinggy.io). That's too short for a recording session with retries.

*serveo.* `ssh -R 80:localhost:3000 serveo.net`, no account. The free plan shows an "interstitial warning" page, removed by Pro at $60 a year (serveo.net). That's the same risk as zrok's, and the status code isn't documented.

*ngrok*, for completeness, because the brief asked. The free plan gives "one automatically assigned dev domain (for example, `your-assigned-name.ngrok-free.app`)". Its browser warning applies to "HTML browser traffic" and not to programmatic callers (ngrok.com/docs free plan limits). Razorpay lists only `ngrok.io`. No primary source says whether `ngrok-free.app`, `ngrok.app` or `ngrok.dev` is refused.

| Tunnel | Free | Account | Stable URL | Interstitial risk | Razorpay doc |
|---|---|---|---|---|---|
| zrok (`*.shares.zrok.io`; corrected by the P0 run) | yes, 5 GB a day | yes (myzrok.io) | yes, reserved name | **yes** for unverified free accounts; answers 200 | **suggested** |
| Cloudflare Quick Tunnel (`*.trycloudflare.com`) | yes | no | no, new each run | none documented | not listed |
| Tailscale Funnel (`*.ts.net`) | yes, Personal plan | yes | yes | none documented | not listed |
| localhost.run (`*.lhr.life`) | yes, speed-limited | no | no, unless signed up | none documented | not listed |
| pinggy (`*.pinggy-free.link`) | 60 min a session | no | no | not stated | not listed |
| serveo (`*.serveo.net`) | yes | no | free custom subdomain | **yes** on free | not listed |
| ngrok (`*.ngrok-free.app`) | yes, 20k requests a month | yes | yes, one dev domain | browsers only | `ngrok.io` refused; the rest not stated |

**What the design should change**
- [hosting.md](../design/hosting.md) "HTTPS on a stable URL": name zrok with a reserved name for the demo laptop, with Cloudflare Quick Tunnel as the fallback. Name Tailscale Funnel as the fallback when a stable URL matters.
- [operations.md](../design/operations.md) `doctor --url`: besides "answers", POST an empty body to the public URL. Fail if the reply is 200 with an HTML body, because that's a tunnel interstitial, not our receiver. Our receiver answers 400 to an unsigned POST.
- [edge-cases.md](../design/edge-cases.md): new row, "Tunnel interstitial answers 200 to Razorpay": the event is lost to webhooks and returns only through the sweeper. Detected by `doctor --url` and by the H2 "sweep found an event older than 10 min" alert.
- [razorpay-first-brief.md](../design/razorpay-first-brief.md) "not yet tried": the refused list is longer than the brief says (11 testing hosts plus local names), and ports are limited to 80 and 443.

**Open point / Spike** (owner's laptop, Windows 11, test mode):
1. Install zrok (`windows.mdx` steps), sign up at myzrok.io, then `zrok2 enable <your_account_token>`, `zrok2 status`.
2. `zrok2 create name -n public wsync-demo`, then, with the receiver on port 8080, `zrok2 share public localhost:8080 -n public:wsync-demo`.
3. In Dashboard test mode, add webhook `https://wsync-demo.shares.zrok.io/webhooks/razorpay` (OTP `754081`). Record whether it saves or the exact error text.
4. Pay one test Payment Link. In the Dashboard log, open the delivery's **Response**. If it's zrok's HTML page rather than our receiver's reply, the interstitial caught it. Note the request's `User-Agent` either way.
5. If caught: either add a card at myzrok.io and repeat, or switch to `cloudflared tunnel --url http://localhost:8080`, edit the webhook URL to the printed `https://<random>.trycloudflare.com/webhooks/razorpay`, and repeat step 4.
6. Optional: try saving a webhook at an `ngrok-free.app` URL and at a `ts.net` Funnel URL, and record whether each is accepted. That settles the unlisted hosts.

URLs: https://razorpay.com/docs/webhooks/validate-test/, https://razorpay.com/docs/webhooks/, https://razorpay.com/docs/webhooks/faqs/, https://razorpay.com/docs/webhooks/setup-edit-payments/, https://razorpay.com/docs/security/whitelists/, https://github.com/openziti/zrok/tree/main/website/docs (get-started, how-tos/shares/manage-reserved-names.md, how-tos/install/windows.mdx, myzrok/service-limits.md, how-tos/migration/migrate-v1-to-v2.md), https://github.com/openziti/zrok/blob/main/endpoints/proxyUi/interstitial.go, https://github.com/openziti/zrok/blob/main/endpoints/publicProxy/http.go, https://github.com/openziti/zrok/blob/main/etc/frontend.yml, https://zrok.io/pricing/, https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/, https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/, https://tailscale.com/kb/1223/funnel, https://tailscale.com/kb/1311/tailscale-funnel, https://localhost.run/docs/, https://localhost.run/docs/forever-free/, https://pinggy.io/docs/, https://pinggy.io/, https://serveo.net/, https://ngrok.com/docs/pricing-limits/free-plan-limits/

## Q5: fee and tax in test mode

**Question.** On a test-mode payment, are `fee` and `tax` filled in, and at which point (authorized or captured)? Do test and live fees differ, by the docs? Do the `payment.authorized` and `payment.captured` webhooks carry fee and tax?

**Verdict: filled in at capture. Null before. The test rate is not the live rate.**

**What the sources say.** The payments entity page defines `fee` as "Fee (including GST) charged by Razorpay" and `tax` as "GST charged for the payment" (as in G41). Its captured sample has `fee` 198 and `tax` 0, and its failed sample has both null. The capture API's response sample, right after capture, has `fee` 1 and `tax` 0.

The payment webhook samples tell the same story. All four `payment.authorized` samples (netbanking, card, wallet, UPI) have `fee` and `tax` null. Three `payment.captured` samples have `fee` 2 and `tax` 0. The **card** `payment.captured` sample has both null. The `order.paid` and `payment_link.paid` samples embed a captured payment with a fee (2/0, and 24/4 in one link sample).

The payments FAQ: "18% GST is charged on the fee deducted for all payment methods except domestic card transactions of amount <= ₹2,000." It also says test mode is simulated and "No money is deducted". No page says test-mode fees are computed differently from live ones, or the same. That is an absence, not a statement. The pricing page advertises "2% + GST" on the standard plan, with a "Platform fee 2.15% + GST" on some methods.

Own runs, both netbanking:
- ₹1 (100 paise): `created` with `fee`/`tax` null; then `captured` with `fee` 2, `tax` 0. GST on a fee that small rounds to 0.
- ₹500 (50000 paise): `created` with nulls; then `captured` with `fee` 1298 and `tax` 198. So the base fee was 1100 (2.2%) and the GST 198 (18% of 1100), and `fee` includes `tax`, as G41 says. Net is 48702.
- A poll of `GET /v1/payments` every few seconds never caught the payment in `authorized`. Under auto-capture, test payments pass through it in seconds, so fee at `authorized` wasn't seen first-hand.

**What the design should change**
- [sources.md](../design/sources.md) "Fetch": take `fee` and `tax` only from the fetch, never from the webhook body, as fetch mode already does. A `captured` payment can still show `fee: null`, as the docs' card sample does. Store null as "not known yet", not 0. Add "captured with fee null" to the hourly re-fetch list.
- [showcase.md](../design/showcase.md) and the README's honest limits: test-mode fees are illustrative. This run charged 2.2% + GST, while the pricing page says 2%. A buyer's live plan sets the real figure. A GST of 0 is correct for domestic card payments up to ₹2,000, and for tiny amounts.
- The demo and GIF amounts should be ₹100 or more by netbanking, so fee, GST and net all show non-zero values.
- [testing.md](../design/testing.md) fakes: FakeRazorpay should replay nulls while `created` and `authorized`, values once `captured`, and the documented `captured` sample with null fee.

**Open point / Spike**
- On the laptop: set **manual capture** in the test Dashboard, pay one order with route 1 and `GET` the `authorized` payment for its `fee`/`tax`. Then capture it with `POST /v1/payments/{id}/capture` and `GET` it again. With the webhook connected, record both payloads' `fee`/`tax`. This settles "fee at authorized" first-hand and gives the fake real bytes.

URLs: https://razorpay.com/docs/api/payments/entity/, https://razorpay.com/docs/api/payments/capture/, https://razorpay.com/docs/webhooks/payments/ (formerly /docs/webhooks/payloads/payments/), https://razorpay.com/docs/webhooks/orders/, https://razorpay.com/docs/webhooks/payment-links/, https://razorpay.com/docs/payments/payments/faqs/, https://razorpay.com/pricing/, https://github.com/razorpay/razorpay-python/tree/master/tests/mocks

## Q6: refunds and disputes in test mode

**Question.** Can a test-mode refund be made to fail? Do test refunds go `pending` → `processed` at once or later, and what is `speed`? Can a dispute be created in test mode? Which webhooks fire? If neither can be produced, what must the tests use instead?

**Verdict: settled. Neither a failed refund nor a dispute can be produced in test mode by any documented means.** A refund itself can be made in test mode. Tests use the documented sample payloads.

**What the sources say.**

*Test mode in general.* The "Test and Live Modes" page says test mode is "a replica of your account in a sandbox environment" and that "Both modes provide the same functionalities, except that real payments cannot be accepted in **Test** mode." The test card and UPI pages offer ways to make a **payment** fail (error-scenario cards, `failure@razorpay`, and the mock bank page's Success and Failure buttons). None of them mentions refunds or disputes. A search of the downloaded refund, dispute and test pages for "test mode", "sandbox", "simulat" and "mock" found nothing about refunds or disputes.

*Refund speed and timing.* A refund is `pending` ("Razorpay is attempting to process the refund"), `processed` ("the final state") or `failed` (refunds entity page). `speed` in the request is `normal` (default) or `optimum`. `speed_requested` echoes it, and `speed_processed` is `instant` or `normal`. Both appear "only if the `speed` parameter is set in the refund request" (entity page). `optimum` tries IMPS, NEFT or UPI and falls back to normal speed (refund-speed page). The fallback fires `refund.speed_changed` (refunds webhooks page). The refunds FAQ says an instant refund "will typically show a `processed` status immediately or shortly after", while a normal refund's "initial API response might return a **`pending`** status". It also says the final status may be "Processed, Failed or Reversed", though "reversed" isn't in the entity's status list. No page says how a refund behaves in **test** mode. The "Create a Normal Refund" sample response shows `"status": "processed"` with `speed_processed: normal` straight away. That is a sample, not a statement about test mode. The overview page also warns that "Razorpay moves a refund to the `processed` state before receiving the ARN/RRN", so `acquirer_data` can fill in after `processed`.

*When a refund fails.* The refunds overview names only two causes: normal refunds of payments "more than 6 months old", and instant refunds failing "because of customer's account or bank-related issues". The "Handle Refund Errors" page shows the 6-month case as a `BAD_REQUEST_ERROR` returned **at creation**: no refund entity is made and no `refund.failed` fires. A failed instant refund "takes 5-7 working days" at normal speed (instant refunds page). It falls back rather than ending `failed`. Neither cause can be set up in test mode: there are no 6-month-old test payments, and no bank to fail.

*Disputes.* The disputes overview says a dispute "can be initiated" by the issuing bank or by the customer through their bank. The Dashboard page offers only View, Accept, Contest and Download. The disputes API has GET list, GET one, POST accept and PATCH contest, with **no create endpoint** (Disputes API index). The overview page's meta description says "Create Disputes and Chargebacks from the Razorpay Dashboard", but the body describes no way to do so. No page mentions a test card, a "simulate dispute" button or a test-mode dispute. Our run found `GET /v1/disputes` works in test mode and returns an empty collection. The "chargeback" test-integration pages under Standard and Custom Checkout are ordinary checkout test pages: they list the same test cards and say nothing about disputes.

*Dispute webhooks* (dispute subscribe page and the all-events page): `payment.dispute.created`, `.won`, `.lost`, `.closed`, `.under_review` and `.action_required`. Each sample payload carries both a `payment` and a `dispute` entity (dispute webhooks page). The dispute's `status` is `open`, `under_review`, `won`, `lost` or `closed`. `phase` is `fraud`, `retrieval`, `chargeback`, `pre_arbitration` or `arbitration` (disputes entity page). `action_required` has no matching `status` value.

*Refund webhooks* (refund webhooks page): `refund.created`, `refund.processed`, `refund.failed` and `refund.speed_changed`. Each sample carries both a `refund` and a `payment` entity. The `refund.created` sample already shows `status: processed`, so the event name doesn't tell the state; the entity does.

**What the design should change**
- [testing.md](../design/testing.md): `refund.failed`, `refund.speed_changed` and all six `payment.dispute.*` events are tested **only on fakes**, built from the documented sample payloads. They are re-serialised and signed with the test secret by our own fixture code. Name the source page beside each fixture. A real test-mode run covers `refund.created` and `refund.processed` only.
- [testing.md](../design/testing.md): the fake `GET /v1/disputes` returns `{"entity":"collection","has_more":false}` with **no `items` or `count`** when empty, as the real API did. That is a different shape from payments and refunds (see API runs). Parsers must treat a missing `items` as empty.
- [sources.md](../design/sources.md) "Handled event types": add `refund.speed_changed`, which maps to the same refund-driven re-fetch. Say that `refund.failed` from a normal refund is very rare: the 6-month case is refused at creation.
- The README's "honest limits" ([showcase.md](../design/showcase.md)): disputes and failed refunds are shown from Razorpay's sample payloads, because Razorpay test mode can't produce them.
- [edge-cases.md](../design/edge-cases.md): a refund reaches `processed` before its ARN arrives. `acquirer_data` must not be part of a state hash, or each ARN fill-in would make a new synthetic event.

**Open point / Spike**
- S-Q6a (laptop): refund a captured test payment twice, once with no `speed` and once with `speed=optimum`. Record the create response's `status`, how long until `processed`, and which webhooks arrive. That settles the test-mode timing the docs leave open.
- S-Q6b (owner): ask Razorpay support whether a test-mode dispute can be raised on the owner's account. This is worth one email. Until an answer arrives, disputes stay fake-only.

URLs: https://razorpay.com/docs/payments/dashboard/test-live-modes/, https://razorpay.com/docs/payments/payments/test-card-details/, https://razorpay.com/docs/payments/payments/test-upi-details/, https://razorpay.com/docs/payments/refunds/, https://razorpay.com/docs/payments/refunds/instant/, https://razorpay.com/docs/payments/refunds/normal/, https://razorpay.com/docs/payments/refunds/refund-speed/, https://razorpay.com/docs/payments/refunds/errors/, https://razorpay.com/docs/payments/refunds/faqs/, https://razorpay.com/docs/api/refunds/entity/, https://razorpay.com/docs/api/refunds/create-normal/, https://razorpay.com/docs/webhooks/refunds/, https://razorpay.com/docs/payments/disputes/, https://razorpay.com/docs/payments/disputes/dashboard/, https://razorpay.com/docs/payments/disputes/subscribe-to-webhooks/, https://razorpay.com/docs/api/disputes/, https://razorpay.com/docs/api/disputes/entity/, https://razorpay.com/docs/webhooks/disputes/, https://razorpay.com/docs/payments/payment-gateway/web-integration/standard/chargeback/test-integration/

## Q7: the sweeper for a source with no events list

**Question.** Using primary docs, establish: the payment lifecycle and which fields change after creation; how long a payment can stay `authorized`; how long refunds stay pending; whether any endpoint lists by status or update time; the complete webhook event list; and rate limits. Then recommend what the checkpoint means, the overlap and the re-fetch window.

**Verdict: settled, and the current sketch has a gap.** A `failed` payment can turn `authorized` up to 3 days after creation. The hourly re-fetch of `authorized` payments only won't see it. No endpoint lists by update time, and rate limits aren't published.

**What the sources say.**

*(a) Lifecycle and changing fields.* The "About Payments" page lists the states `created` → `authorized` → `captured` → `refunded`, plus `failed`. "Payment in this state [authorized] is auto-refunded to the customer if not captured within 3 days of creation." The late-authorisation page adds a path the state diagram doesn't show. With no bank response, a payment stays `created` for 10 minutes, "is later marked as **Failed**", and then "Razorpay polls the bank at various intervals for 3 days, from the payment creation day. During this time, if our system receives the payment status from the bank as **Successful**, the payment is marked as `Authorized`." So **`failed` is not final for 3 days**. Under less than 0.5% of payments are late-authorised (same page).

The payments entity has **no `updated_at`**, and neither do the refunds or disputes entities (searched each entity page). It has **no dispute fields** either. Fields that can change after creation, with the source for each:
- `status`, from the lifecycle above. `captured` (boolean) flips at capture.
- `fee` and `tax`: `null` in every `payment.authorized` sample and set in the `payment.captured` sample (`fee: 2, tax: 0`). One `payment.captured` sample still shows `fee: null` (payments webhooks page). So the fee arrives at or after capture.
- `amount_refunded` and `refund_status` (`null`, `partial`, `full`) change with refunds (entity page).
- `error_code`, `error_description`, `error_source`, `error_step` and `error_reason` are set when a payment fails. No page says whether a late authorisation clears them. That is undocumented, so they belong in the hash.
- `notes` can be changed by "Update the Payment" (PATCH; razorpay-mcp-server lists `update_payment` as "Update the notes field of a Payment").

A refund's `status` and `speed_processed` change, and `acquirer_data` fills in late (Q6). A dispute's `status`, `phase`, `amount_deducted` (0 "unless the status of dispute is updated to `lost`") and `respond_by` change (disputes entity page). The payments webhooks page warns that "The payload for a Webhook is a snapshot of the entity when the event occurred" and that a payment can already be `captured` when `payment.authorized` fires.

*(b) How long `authorized` lasts.* "Payment Capture Settings": auto-capture of every payment authorised within 3 days is "the default setting for all customers". Custom auto-capture and manual-capture timeouts run from 12 minutes to 3 days (the default and the maximum). "You must ensure that all payments in the authorized state are moved to the captured state within 3 days of creation… payments that are not captured within this period will be refunded automatically", always at **normal** speed. The settings "are applicable only for payments created using the Orders API", and Direct Settlement merchants are always auto-captured. The late-authorisation handling page says late-authorised payments not captured "are auto-refunded in 5 days" ("within 5 working days (without considering the bank's processing time)"). So an `authorized` payment lasts at most 3 days from creation. The auto-refund that follows can take until about day 5 to show. No page says the status the payment then shows (presumably `refunded`, with a refund entity); that is an open point.

*(c) How long refunds stay pending.* Normal speed: "processed within 7-10 working days" (normal refunds page), or 5-7 business days (refund-speed page). By method: cards 5-10 days, netbanking 2-10, wallet 0-3 and UPI 2-7 (normal refunds page). Instant (`optimum`): "Up to 500000" is "credited immediately". Above that and up to 10000000, NEFT within 3-4 business hours, or up to 1 business day. Above 10000000, normal speed. The page gives no unit; IMPS's ₹5 lakh limit suggests rupees. A failed instant refund falls back to normal and takes 5-7 working days (instant refunds page). These are times to reach the customer. `processed` usually comes earlier, "before receiving the ARN/RRN" (refunds overview). So a refund can stay `pending` for about 14 calendar days at worst by the docs.

*(d) Listing by status or update time.* Documented filters are `from`, `to`, `count` and `skip` only, all on **creation** time. Our runs found **undocumented** filters that `/v1/payments` accepts and validates: `status`, `order_id`, `email`, `contact`, `international`, `notes`, `recurring` and `expand[]` (one of card, emi, transaction, transaction.settlement, refunds, offers, token, settlement). It rejects `updated_at`, `updated_from`, `updated_to`, `created_at`, `captured`, `method`, `refund_status`, `dispute_id`, `receipt`, `id`, `customer_id` and `entity_id` as "not required and should not be sent". **There is no update-time filter on payments.** `/v1/refunds` accepts unknown parameters silently, so its acceptance of `updated_from` proves nothing. `/v1/disputes` validates `count` (≤ 100), `skip`, `from`/`to` (range-checked) and `status` (open, under_review, won, lost, closed), and rejects unknown names. That answers G39's open point about whether the parameters are accepted. Whether they filter needs disputes to exist. `GET /v1/events` returns 404 "no Route matched with those values". Other candidates:
- `GET /v1/settlements/recon/combined?year&month[&day]` lists "all transactions such as payments, refunds, transfers and adjustments settled to your account on a particular day or month", with `count` 1-1000, `skip`, `type` and `dispute_id` (settlement recon page). It is keyed by **settlement** day, so it sees only settled money, days after the change. It is a reconciliation source, not a change feed.
- `GET /v1/payments/downtimes` returned 9 items in test mode. It reports payment-method downtime, not payment changes.

So there is **no change feed**. A change to a payment can only be found by re-reading it, by id or inside a creation-time list.

*(e) Webhook events relevant to the ledger* (all-events page; payments, refunds, disputes and orders pages):
- Payments: `payment.authorized`, `payment.captured`, `payment.failed`.
- Orders: `order.paid` (payment captured, with order and payment entities).
- Refunds: `refund.created`, `refund.processed`, `refund.failed`, `refund.speed_changed`.
- Disputes: `payment.dispute.created`, `.won`, `.lost`, `.closed`, `.under_review`, `.action_required`.
- Not ledger events: `payment.downtime.started`, `.updated` and `.resolved`; `settlement.processed`; `invoice.*`; `payment_link.*`; `qr_code.*`.

There is no `payment.refunded` and no `payment.created` event.

*(f) Rate limits.* "Understand Razorpay APIs" says only that a rate limiter limits requests "within a time frame". It advises watching for HTTP 429 with exponential or stepped backoff and jitter. The common-errors page shows the throttled body as `"code": "BAD_REQUEST_ERROR", "description": "Too many requests"`. It says to "Reduce the frequency of API calls, particularly for polling-based implementations" and that increases are "reviewed case-by-case". **No number is published.** Our 200 and 400 responses carried no rate-limit headers. They do carry a `Date` header that matched the machine clock to the second, so the "checkpoint on the source's clock" rule from the Stripe design works for Razorpay too. Secondary, not counted: drdroid.io says each account gets "a specific number of API requests per minute", also without a number.

**What the design should change**
- [sweeper.md](../design/sweeper.md) "Razorpay: polling" and [sources.md](../design/sources.md) "Sweep and polling": replace step 2 (re-fetch ledger payments that are `authorized` or have an unprocessed refund) with the **re-list** described under Recommendation. The current rule misses (1) `failed` → `authorized` late authorisations, (2) `created` → anything, and (3) fees arriving after capture. A re-list returns full entities, so one call per 100 payments replaces one call per payment.
- [sources.md](../design/sources.md) "Fetch": map the throttle by **HTTP 429**, not by `error.code`, because the body says `BAD_REQUEST_ERROR`.
- [sources.md](../design/sources.md) "API reads per month": add a Razorpay row (see the cost line below). Note that Razorpay publishes no allowance, so the backoff path is the only guard.
- [edge-cases.md](../design/edge-cases.md): add "payment `failed`, then `authorized` up to 3 days later (late authorisation)" and "`authorized` payment auto-refunded after the capture timeout (≤ 3 days, refund visible within about 5 working days)".
- [verify.md](../design/verify.md): nightly verify (30 days) remains the only catch for a refund still `pending` after 21 days and for any undocumented late change.

**Recommendation**

*Checkpoint.* One checkpoint per list (payments, refunds), on Razorpay's clock (the `Date` header). It means: **every payment (or refund) with `created_at` < checkpoint has been listed and stored at least once.** A slice runs from `start = checkpoint` to `end = min(start + 6 h, source_now − 60 s)`. It is requested as `from = start − 600`, `to = end`, keeping `created_at < end` on our side, and paged as in Q3(d). The checkpoint moves to `end` only after the whole slice is stored, exactly as for Stripe. The checkpoint is about **coverage**, not state.

*Overlap: 10 minutes.* No page reports listing lag, and `created_at` never moves. The overlap only guards against an item becoming listable a little after its timestamp and against boundary inclusivity. Ten minutes costs nothing (the same page) and keeps one code path with Stripe. A longer overlap doesn't help with state changes; the re-list below does that.

*State re-list window (replaces the hourly re-fetch).* Hourly, on the source clock:
1. `GET /v1/payments` with `from = source_now − 7 days`, all pages. Seven days covers late authorisation (3 days), the capture timeout (3 days), the late-authorised auto-refund ("within 5 working days") and the fee arriving at capture. Each payment whose state hash differs from the ledger's becomes a synthetic event and a ledger job.
2. `GET /v1/refunds` with `from = source_now − 21 days`, all pages. Twenty-one days covers normal refunds' 7-10 working days with margin. Each refund whose hash changed enqueues a job for its `payment_id`.
3. `GET /v1/disputes`, all pages (no `from`). Disputes are few and can run for months through pre-arbitration and arbitration. Dispute state comes only from here and from dispute events, as now (F10).

*State hash* (canonical JSON of these fields, so the synthetic id is `sweep:<entity id>:<hash>`):
- payment: `status`, `captured`, `amount`, `amount_refunded`, `refund_status`, `fee`, `tax`, `error_code`, `error_reason`, and `notes` if the sheet shows metadata.
- refund: `status`, `amount`, `speed_processed`.
- dispute: `status`, `phase`, `amount_deducted`.

Leave out `acquirer_data`, card details and `respond_by`, which change without changing the ledger row.

*Synthetic event types* (needed by the silent-webhook check):
- payment: `authorized` → `payment.authorized`, `captured` → `payment.captured`, `failed` → `payment.failed`, `refunded` → `payment.captured`, with the refund carried by the refund list. `created` → none: no row, and not stored.
- refund: `pending` → `refund.created`, `processed` → `refund.processed`, `failed` → `refund.failed`.
- dispute: `open` → `payment.dispute.created`, and `under_review`, `won`, `lost` and `closed` → the event of the same name.

`payment.dispute.action_required`, `refund.speed_changed` and `order.paid` have no state of their own and are never synthesised.

*Cost* for a buyer with 100 payments a month: slices every 10 minutes, about 2 lists × 4,400 = 8,800 calls a month. Most of these hit an empty page and could run every 30 minutes for Razorpay. Hourly re-lists add 3 × 720 ≈ 2,200. That makes about 11,000 calls a month, or about 4,300 with 30-minute slices. Nothing published bounds this, so 429 backoff is the guard.

**Open point / Spike**
- S-Q7a (laptop): after S-Q3, leave one test payment `authorized` with manual capture through an Order with `payment_capture` off, and fetch it daily for 6 days. Record when its status changes, to what, and whether a refund entity and `refund.created` appear. That settles the auto-refund status the docs leave unsaid.
- S-Q7b (laptop): check that `fee`/`tax` are filled in on a captured test payment, and when. That is shared with Q5.
- S-Q7c: once one dispute exists anywhere, check that `/v1/disputes?status=open` and `from`/`to` actually filter.

URLs: https://razorpay.com/docs/payments/payments/, https://razorpay.com/docs/payments/payments/late-authorisation/, https://razorpay.com/docs/payments/payments/late-authorisation/handle/, https://razorpay.com/docs/payments/payments/capture-settings/, https://razorpay.com/docs/api/payments/entity/, https://razorpay.com/docs/webhooks/payments/, https://razorpay.com/docs/webhooks/all/, https://razorpay.com/docs/webhooks/orders/, https://razorpay.com/docs/webhooks/refunds/, https://razorpay.com/docs/webhooks/disputes/, https://razorpay.com/docs/api/disputes/entity/, https://razorpay.com/docs/payments/refunds/normal/, https://razorpay.com/docs/payments/refunds/instant/, https://razorpay.com/docs/api/settlements/fetch-recon/, https://razorpay.com/docs/api/understand/ (Rate Limiting), https://razorpay.com/docs/errors/common/, https://github.com/razorpay/razorpay-mcp-server/blob/main/pkg/razorpay/payments.go. Secondary, not counted: https://drdroid.io/integration-diagnosis-knowledge/razorpay-rate-limit-exceeded/

## Q8: mode

**Question.** Do Razorpay deliveries or API answers say whether they are test or live? Do the modes have separate webhooks and secrets? What can `doctor` check beyond the key prefix?

**Verdict: confirmed.** No payload field or documented header carries the mode. The key prefix is the only reliable check. `doctor` can add two best-effort checks. One lists this mode's webhooks through the undocumented-for-merchants `GET /v1/webhooks`. The other relies on the fact that a payment from the other mode can't be fetched.

**What the sources say.** All 31 payment payload samples on webhooks/payments have exactly these top-level keys: `entity` ("event"), `account_id` (`acc_…`), `event`, `contains`, `payload` and `created_at`. There's no `mode`, `livemode` or `test` field (parsed with a script on 2026-10-02). The validate-test page says "the payload structure remains the same in the **Live** and **Test** modes". The only headers the docs name are `X-Razorpay-Signature` and `x-razorpay-event-id` (validate-test). No page says whether `account_id` differs between modes. No id prefix marks a test entity: samples use `pay_`, `order_` and `acc_` in both. Test mode is "a replica of your account in a sandbox environment", and "Transactions and entities created in the test mode do not appear in the live mode". There is a "Separate Set of API Keys in Test and Live Mode" (payments/dashboard/test-live-modes). Webhooks are set up per mode, with "separate URLs for **Live** mode and **Test** mode", and "A **Test** mode webhook receives events for your test transactions" (webhooks overview). The secret is entered by the owner per webhook and "does not need to be the Razorpay API key secret" (setup-edit-payments). Nothing stops the owner typing the same secret for a test and a live webhook. So the statement in edge-cases A9 that "each mode has its own secret" holds for Stripe, but for Razorpay only by convention. API responses carry no mode header either. Our runs returned only `Content-Type`, `Cache-Control`, `Expires`, `Pragma`, `Strict-Transport-Security`, `X-Frame-Options`, `X-Pam` and `X-Xss-Protection`.

The webhooks API is documented only for partners, as `/v2/accounts/:account_id/webhooks` for a sub-merchant (api/partners/webhooks; razorpay-python `documents/webhook.md` shows only the `accountId` form). But razorpay-python's `Webhook.all()` calls `/v1/webhooks` when `account_id` is `None`, and the same goes for create, fetch and edit (`razorpay/resources/webhook.py`). Our test key got **200** from `GET /v1/webhooks` with `{"entity":"collection","count":0,"items":[]}`; the test account has no webhook yet. `GET /v2/accounts/me/webhooks` gave 404 "no Route matched with those values". The partner entity sample shows the fields such a listing would carry: `url`, `active`, `events`, `secret_exists`, `disabled_at` and `alert_email` (api/partners/webhooks/fetch-all). A fetch of an unknown payment id with our test key gave 400 `BAD_REQUEST_ERROR`, "The id provided does not exist". A live payment fetched with test keys should give the same, because entities don't cross modes. That isn't tried, since there is no live account.

**What the design should change**
- [sources.md](../design/sources.md) "Mode" row: keep the key-prefix check as the only hard check. Add: "a payment from the other mode can't be fetched (400 'The id provided does not exist'); the ledger worker marks such a job dead after a few attempts with reason `not_found`, naming a possible mode mix-up". Add to `check_setup()`: list `GET /v1/webhooks` with this deployment's keys and **warn** (not fail) when no active webhook has our URL, when a handled event isn't ticked, when `secret_exists` is false or `disabled_at` isn't 0. Say the endpoint is undocumented for merchants, so a 4xx from it is reported as "can't check", never as a failure.
- [operations.md](../design/operations.md) doctor table: add a Razorpay row next to the Stripe endpoint row with the checks above.
- [edge-cases.md](../design/edge-cases.md) A9: for Razorpay, "each mode has its own secret" is only true if the owner types different ones. The runbook and README quickstart should say to use a different webhook secret for test and live.
- [receiver.md](../design/receiver.md) row 7 stands: Razorpay events carry no mode; the prefix is checked at startup.

**Open point / Spike** (owner's laptop):
1. After creating the test-mode webhook in Q4, run `GET /v1/webhooks` with the test key and confirm it lists that webhook with `url`, `active`, `events`, `secret_exists` and `disabled_at`.
2. If a live key ever exists, run the same call with it and confirm the test webhook is **not** listed, so the listing is per mode.
3. From one recorded test delivery, note every request header (Dashboard log › Request Headers). Confirm none names the mode.
4. Note the delivery's `account_id`. If a live account is ever activated, compare it with the live one. This is informational only and not used as a check.

URLs: https://razorpay.com/docs/webhooks/payments/, https://razorpay.com/docs/webhooks/, https://razorpay.com/docs/webhooks/validate-test/, https://razorpay.com/docs/webhooks/setup-edit-payments/, https://razorpay.com/docs/payments/dashboard/test-live-modes/, https://razorpay.com/docs/api/partners/webhooks/, https://razorpay.com/docs/api/partners/webhooks/fetch-all/, https://github.com/razorpay/razorpay-python/blob/master/razorpay/resources/webhook.py, https://github.com/razorpay/razorpay-python/blob/master/documents/webhook.md

## Q9: Stripe without an account

**Question.** The owner is an individual in India, and Stripe is invite-only there. (a) What exactly do Stripe's pages say, has anything changed in 2025-2026, and can a test-only account be opened from India or under another country? (b) What can be built and tested with no account: stripe-mock, the OpenAPI fixtures, stripe-python's signing helpers, the Stripe CLI? (c) Which open Stripe spikes can be settled without an account? (d) What are the routes to real Stripe access later?

**Verdict:**
- **India is still invite-only.** The wording is unchanged and undated. stripe.com/global marks India as **Preview** and sends it to the Sales form, not to sign-up. No Stripe page offers a test-only account to Indian businesses. The Services Agreement defines the account country as the place where you do business, so choosing another country to get test mode would misstate it. Don't do it.
- **A new route exists, untested from India.** Since Stripe CLI v1.42.0 (commit `fa17253`, 22 May 2026), `stripe sandbox create --email …` makes a **temporary sandbox with test keys "without requiring an account"**. The sandbox lasts 7 days, and the CLI sends no country. Claiming it permanently needs a Stripe account, so in India it needs an invite. A 2-3 hour recording spike fits inside 7 days. It is worth one 10-minute try on the owner's laptop.
- **With no account at all**, the following can be built: the adapter, the signature checks (stripe-python signs payloads locally with any secret), and a FakeStripe made from Stripe's documented payloads and the OpenAPI fixtures. stripe-mock is stateless, sends no webhooks and validates loosely, so it is only good for shape checks. `stripe listen` and `stripe trigger` need a login or an API key.
- **Open spikes:** none of the five open Stripe rows can be fully settled without real Stripe. Two (G7, G10) are settled well enough by the docs plus tolerant design. One (G28) is partly settled by unauthenticated calls. G6 can be made irrelevant by a design change. G27 must wait.
- **Later access:** the best route is a buyer inviting the owner as a **team member** to a sandbox. That needs only a Stripe *login*, not a business account. Second best is the buyer's own restricted key (Q85). Connect doesn't help, because connected accounts in India are also invite-only. Stripe Atlas exists ($500, then $100 a year), but it means forming a US company.

**What the sources say.**

*(a) India status.* The support article "Stripe accounts are invite-only in India" says: "Stripe services are invite-only in India. Businesses from India are not able to sign up for a new Stripe account through our website, and instead need to request an invite. We are only able to support a select number of businesses, with a focus on international expansion, for the time being." To request an invite you "reach out to the Sales team" (stripe.com/contact/sales), and the article warns: "We apologize if we're not able to serve your business at this time." It names no criteria for an invite beyond that focus. Existing Indian accounts stay supported. "For Platforms using Stripe in India, we support the addition of new connected accounts by invite only." The page has no date. It says Stripe will "share updates on this page", and on 2026-10-02 it shows none. The India FAQ repeats the same wording.

"How can I open a Stripe account in India?" lists what an invited account needs. An **Individual** gives a PAN, Aadhaar or Voter ID, a website or social profile, an INR bank account and an address. For an Individual, "Accepting international transactions is not available as an option". Only a sole proprietorship or a company can export, with an IEC code and an RBI purpose code. "Once an account has been created, its country cannot be updated to or from India."

On stripe.com/global, India and Indonesia are marked **Preview** and link to the Sales form. Every other country links to `dashboard.stripe.com/register?country=XX`. Secondary, not counted: TechCrunch and Inc42 dated the move to invite-only to May 2024. No primary source shows any change in 2025-2026.

*Test mode without activation.* docs.stripe.com/get-started/account/set-up says: "After creating a Stripe account, you can test Stripe services in a sandbox. To use a service in live mode, verify your business." Test mode needs no activation, but it does need an account, and the India article lets no Indian business create one through the website. No page describes a test-only kind of account at sign-up. The one exception is the CLI sandbox below.

The Stripe Services Agreement (General Terms, last modified 28 September 2026) defines "Stripe Account Country" as "the country or region User selected when opening User's Stripe Account and is the country or region where User's business address … is located, or, in the case of an individual, the country or region where User is doing business". So the country is not a free choice. Section 1.2(a)(ii) also forbids "circumvent[ing] any technical limitations of the Services". The Agreement takes effect "when User first accesses or uses the Services or Stripe Technology", so it binds test-only use too. Sandbox settings do let an *existing* account make a sandbox "with a country different from your live account" (Create an account from scratch, then Business settings › Account details). That needs an account first.

*The temporary CLI sandbox.* docs.stripe.com/cli/sandbox: "If your CLI isn't logged into Stripe, this command provisions a new sandbox with working test API keys, without requiring an account." It goes on: "Your temporary sandbox credentials are saved to your CLI profile so that other commands work without needing to log in", and "Sandbox environments expire after 7 days." The example key is `rkcs_test_…`.

In the source, `pkg/cmd/sandbox.go` posts to `https://ai.stripe.com` after a proof-of-work challenge (L31, L212-233). The request carries only the challenge, an email and an optional name (`pkg/sandbox/sandbox.go` L53-59), so **no country is sent**. If provisioning fails, it falls back to browser sign-up (L221-230), which in India hits the invite wall. `pkg/validators/validate.go` L100 accepts `rkcs` as a key prefix. `pkg/cmd/root.go` L65 tells agents: "If you do not have an account, run `stripe sandbox create`".

The docs don't say which country the sandbox gets. They also don't say whether it works from India, whether an `rkcs_` key can open a `stripe listen` session, or whether a new sandbox can be made after one expires. The platform version, "claimable sandboxes", lists IN among its supported countries and keeps a sandbox for 60 days. It needs a Stripe App on a live account, so it is not for us. Claiming either kind "create[s] a new Stripe account or choose[s] an existing one", which brings back the invite.

*(b) What works with no account.*

**stripe-mock.** The README says it "does not attempt to reproduce the behavior of the real Stripe API at all", that its responses "are completely hardcoded", and that it "is stateless". It is "locked to the latest version of Stripe's API", and test cards that should fail return success instead. Stripe says it is "not planning to add statefulness".

Neither the README nor `server/` nor `main.go` mentions webhooks, so it sends none. Built from source and run here, it showed four more things:
- It accepted any "valid looking" test key, `rk_test_123` included.
- Expansions work, including `expand[]=data.latest_charge.balance_transaction` on a list.
- It **didn't reject** `limit=101`, `created[foo]=1`, an unknown expand path or 21 `types[]`. Stripe's documented limit is 20 (G16).
- After `POST /v1/refunds` on `ch_1`, `GET /v1/charges/ch_1` still showed `amount_refunded: 0`.

It sends `Request-Id: req_123` and a `Date` header (`server/server.go` L270-271).

**OpenAPI.** github.com/stripe/openapi has `openapi/spec3.json` and `openapi/fixtures3.json`. The README calls `/openapi/` "Legacy. v1-only", still updated every release, and recommends `/latest/`. stripe-mock embeds an identical copy of `fixtures3.json`: one fixture for each of 181 resources. The fixtures are single static examples:
- The event is `plan.created`.
- The PaymentIntent is `requires_payment_method` with no `latest_charge`.
- The charge has `amount_refunded: 0` and `payment_intent: null`.
- The refund is `succeeded`, and the dispute is `warning_needs_response`.

The spec's `charge.amount_refunded` description says nothing about failed refunds.

**stripe-python 16.0.0** (`stripe/_webhook.py`):
- `WebhookSignature.generate_signature_header(payload, secret, timestamp=None)` (L119-129) returns `t=…,v1=…` for any body and any secret. Its docstring says "Useful for signing payloads in unit tests." It became public in v15.5.0 (commit `5e03051`, 2026-08-06). Before that the tests used the private `_compute_signature` (L104).
- `generate_test_header_string` is not a stripe-python name; grep finds it nowhere in `stripe/` or `tests/`.
- The helper writes only `v1`. `tests/test_webhook.py` builds headers with other schemes by hand (`_build_header_with_scheme`), and that is how `v0` and multi-`v1` headers can be made.
- Run here, a body signed with `whsec_anything` passed `Webhook.construct_event`. With no tolerance, `verify_header` accepted a header with `t=1`. `construct_event` defaults to 300 s (L64, L71), `verify_header` to `None` (L137), and only past timestamps are checked (L189). This matches G9 and receiver.md check 6.

**Stripe CLI.**
- `listen` calls `Config.Profile.ResolveCredentials` (`pkg/cmd/listen.go` L181, `pkg/config/profile.go` L1104), which needs a login token or an API key.
- Without one, the CLI reports `ErrAPIKeyNotConfigured`, "you have not configured API keys yet" (`pkg/validators/validate.go` L19). It then runs or suggests `stripe login` (`pkg/cmd/root.go` L205-225).
- `trigger` makes real API objects in a sandbox (`pkg/cmd/trigger.go` L53).
- So **neither works offline**. They work with the temporary sandbox's saved keys, according to the docs (not tried).
- The trigger recipes in `pkg/fixtures/triggers/*.json` can be read offline. For example, `charge.dispute.created.json` confirms a PaymentIntent with `pm_card_createDisputeInquiry`. They show which calls make which events, but not the payloads.

*(d) Routes to real access.*

**Team member.** docs.stripe.com/sandboxes says a sandbox owner can "invite another user, such as an implementation partner … to access all sandboxes, or a specific sandbox, without providing them access to your live mode data". There are two ways:
- **Sandbox User** role: "can't view or make changes to anything in your live account"; can create sandboxes and payments in them.
- An invite to one sandbox: if the invitee has no live role, "Stripe automatically assigns them the sandbox user role".

The support article on inviting team members says: "If the team member doesn't have a Stripe login yet, they will be invited to create one and then can access the account." That is a *login*, not a business account. No page says a login is refused by country, but none says it is allowed from India either. Invites expire after 10 days (docs.stripe.com/get-started/account/teams). The Developer role can create secret keys. Sandbox roles are denied only *live* API keys. A secondary summary claimed the invitee "must create … a personal Stripe account"; the primary page says "login".

**Buyer's restricted key (Q85).** This is already designed. It needs nothing from the owner's side, and a buyer can create it in a sandbox (G1).

**Connect.** Being a connected account under someone's platform would still make an Indian account, and those are invite-only, as quoted above.

**Stripe Atlas** (stripe.com/atlas). It forms a Delaware company for "$500 one-time setup fee (includes government fees and your first year of registered agent services)", then "$100 annually after your first year". The page lists no other running costs, and US tax filings and India's own rules for a resident owning a foreign company are not covered here. It is a real company, not a test-account trick. Noted only.

**What the design should change**
- [testing.md](../design/testing.md):
  - Split the recording spike. The **Razorpay spike** runs now. The **Stripe spike** waits for access: first `stripe sandbox create` on the owner's laptop, else a buyer's sandbox.
  - Until it runs, FakeStripe replays **documented** payloads (API reference examples, `fixtures3.json` shapes, CLI trigger recipes) signed with `generate_signature_header`. Mark each one `unrecorded`. This is Q55's Razorpay fallback applied to Stripe.
  - To keep F21's point, give FakeStripe a switch for each open fact, and run the invariants under **both** answers. That covers: a failed refund that does or doesn't lower `amount_refunded` (G6), resend bytes that differ (G7), header variants such as `v0`, two `v1`, and a charset on the content type (G10), list lag from 0 to 10+ minutes (G27), and a missing `Date` (G28).
  - Turn the weekly Stripe real-services job off until a key exists.
  - Use stripe-mock at most as an optional shape smoke test, never as FakeStripe. It is stateless, and it accepted `limit=101`.
- [build-order.md](../design/build-order.md):
  - Take the Stripe account, restricted key and CLI out of P0.
  - Add "Stripe S0: recording spike, 2-3 h, when access exists" in front of the Stripe adapter in the after-pitching table.
  - Note that the 7-day sandbox means the whole spike runs in one sitting and the recordings are saved to `tests/recordings/` before it expires.
- [research-gaps.md](../design/research-gaps.md):
  - Add a "needs Stripe access" note to G6, G7, G10, G27 and G28.
  - G7 and G10: "settled by docs and design; confirm when recorded".
  - G28: add the 2026-10-02 observation (three 401s, each with `Date`).
  - Add a row: "`stripe sandbox create` works from India, and its `rkcs_` key can open `stripe listen`". Check SPIKE (10 minutes, owner's laptop); first slice "Stripe S0".
  - Correct the count. There are **five** open Stripe SPIKE rows, and eight open SPIKE rows in total (G6, G7, G10, G21, G27, G28, G33, G38). The brief's Q9 says "eight open Stripe spikes".
- [sources.md](../design/sources.md): work out the refunded total from the charge's refunds whose `status` is `succeeded` or `pending`, not from `amount_refunded`. G6 then no longer matters, as gaps-stripe.md already suggested. It costs one list call when a refund event arrives.
- [receiver.md](../design/receiver.md): no rule change. Check 4 already accepts parameters and `+json`. Add unit tests for a `v0` signature and for two `v1` values.
- [next-adapters.md](../design/next-adapters.md) or the Stripe part of [sources.md](../design/sources.md): add a short "Stripe access" note listing the routes in order:
  1. The temporary CLI sandbox, for the spike only.
  2. A buyer invites the owner to a sandbox as Sandbox User.
  3. The buyer's own restricted key (Q85).
  4. An invite through Stripe Sales, which is unlikely: individuals can't take international payments, and Stripe favours businesses expanding abroad.
  5. Atlas, noted with its cost.
  The note should also say that no other country is ever picked at sign-up.
- The README's honest limits: until the Stripe spike runs, say "Stripe adapter tested against fakes built from Stripe's documentation, not yet against Stripe".

| Open Stripe spike | Settle without account? | How |
|---|---|---|
| G6 failed refund and `amount_refunded` | No; make it irrelevant | Docs are silent, and stripe-mock is stateless (its refund left `amount_refunded` at 0). Change sources.md to sum refunds by status, and fake both behaviours. Confirm in a sandbox with card 4000000000005126. |
| G7 stable fingerprint fields | Mostly, by docs | Event docs say `data` never changes and `id`, `type`, `created` and `livemode` are fixed. Different bytes are already expected. The byte diff waits for a sandbox. |
| G10 content type and headers | Mostly, by design | Checks 4 and 5 are already tolerant, and the CLI source shows one value per header name. Unit-test the variants. Logging real headers waits for `stripe listen` or an endpoint. |
| G27 events listable within 10 min | No | Only real traffic shows the lag. Keep the 10-minute overlap, and let the fake inject lag. Wait for a sandbox; a day of polling. |
| G28 `Date` header | Partly | On 2026-10-02, 401s from `/v1/events`, `/v1/payment_intents` and `/v1/balance_transactions` each carried `Date`. An authenticated 200 needs a key. Keep the fallback. |
| Spike item: events list order (F03) | Yes, by docs | G26: newest first. Fake it from the docs. |
| Spike item: `charge.updated` timing (Q97) | No | Docs promise the update within 1 hour. Measuring it needs a sandbox. |
| Spike item: UGX and ISK amounts (Q100) | No | The docs contradict themselves. Needs a sandbox charge. |
| Spike item: expand without `balance_read` (G1 open point) | No | Needs a restricted key, which needs a Dashboard. The temporary sandbox's own `rkcs_` key can't show this. |
| Spike item: dispute states (G8) | Yes, by docs | G8 is closed. Recording real payloads waits for a sandbox. |

URLs: https://support.stripe.com/questions/stripe-accounts-are-invite-only-in-india, https://support.stripe.com/questions/india-faq, https://support.stripe.com/questions/how-can-i-open-a-stripe-account-in-india, https://stripe.com/global, https://stripe.com/legal/ssa, https://docs.stripe.com/get-started/account/set-up, https://docs.stripe.com/sandboxes, https://docs.stripe.com/sandboxes/dashboard/sandbox-settings, https://docs.stripe.com/sandboxes/dashboard/manage-access, https://docs.stripe.com/sandboxes/claimable-sandboxes, https://docs.stripe.com/cli/sandbox, https://docs.stripe.com/get-started/account/teams, https://docs.stripe.com/get-started/account/teams/roles, https://support.stripe.com/questions/invite-team-members-or-developers-to-access-your-stripe-account, https://stripe.com/atlas, https://github.com/stripe/stripe-cli/blob/master/pkg/cmd/sandbox.go, https://github.com/stripe/stripe-cli/blob/master/pkg/sandbox/sandbox.go, https://github.com/stripe/stripe-cli/blob/master/pkg/cmd/root.go, https://github.com/stripe/stripe-cli/blob/master/pkg/cmd/listen.go, https://github.com/stripe/stripe-cli/blob/master/pkg/config/profile.go, https://github.com/stripe/stripe-cli/blob/master/pkg/validators/validate.go, https://github.com/stripe/stripe-cli/tree/master/pkg/fixtures/triggers, https://github.com/stripe/stripe-mock/blob/master/README.md, https://github.com/stripe/stripe-mock/blob/master/server/server.go, https://github.com/stripe/openapi/blob/master/openapi/fixtures3.json, https://github.com/stripe/openapi/blob/master/openapi/spec3.json, https://github.com/stripe/stripe-python/blob/master/stripe/_webhook.py, https://github.com/stripe/stripe-python/blob/master/tests/test_webhook.py; secondary, not counted: https://techcrunch.com/?p=2787172, https://inc42.com/buzz/us-payments-giant-stripe-goes-invite-only-in-india

## Q10: build order

**Question.** Razorpay's 8-12 h moves in front of Stripe. Which of P0-P6 change?

**Verdict: internal; no outside fact needed.** The facts above that bear on it: P0's accounts become Razorpay test keys, a Google service account and a throwaway sheet (no Stripe CLI, Q9); the recording spike splits into a Razorpay half that runs now and a Stripe half that waits for access (Q9); test payments are scripted, so `wsync demo` needs Playwright as a demo-only extra (Q1); the minimal sweeper becomes a creation-time poll plus an hourly re-list (Q3, Q7); real deliveries need a tunnel on the owner's laptop (Q4). Round 11 decides the order.

## Sheets checks run in the same session: G21 and G18

Run 2026-10-02 against the owner's throwaway spreadsheet with the service account, in a temporary tab that was deleted afterwards; the sheet's locale (`en_GB`) and time zone (`Asia/Calcutta`) were restored.

**G21, date serials and locale: confirmed.** A `numberValue` serial with a `DATE_TIME` pattern `yyyy-mm-dd hh:mm:ss` was written, then the spreadsheet's locale and time zone were changed with `updateSpreadsheetProperties` to `en_US`/`America/New_York`, `de_DE`/`Europe/Berlin`, `ja_JP`/`Asia/Tokyo`, `hi_IN`/`Asia/Kolkata` and `fr_FR`/`Asia/Kolkata`. Each time `effectiveValue` stayed the same number and `formattedValue` the same text: neither the locale nor the spreadsheet time zone re-parses or shifts a stored serial, so the time zone belongs in the header, as [Q87](../design/grilling-log.md) and [Q95](../design/grilling-log.md) say. A second run wrote serials computed from whole Unix seconds (`(seconds + offset) / 86400 + 25569`): they display the exact second, and the same value plus half a second displays the next second, so Sheets rounds to the nearest second. The writer computes serials from whole seconds and adds nothing.

**G18's open point: confirmed.** One `getByDataFilter` call with two `GridRange` filters on one `sheetId` (row 0, and column A) returned the grid size and **two** `data` blocks, one per filter, in request order. A filter naming a `sheetId` that doesn't exist fails the whole call with HTTP 400 `INVALID_ARGUMENT`, "No sheet with id: …", which the flusher reads as "tab deleted" (pause-class).

## API runs

All against `https://api.razorpay.com` with the owner's test keys, passed from the environment through `curl -u`; the prefix was checked as `rzp_test_` first. No key value or personal data was printed or stored. Two calls failed once with a connection reset through the session's proxy and succeeded on retry, so the sweeper must treat a reset as transient.

### Test-mode writes (Q1, Q5)

All calls used the owner's test keys (prefix checked as `rzp_test_` first). No secret was printed or stored. The dummy contact details used (`demo@example.com`, a made-up `90000…` number) belong to nobody. All times are 2 October 2026.

#### Reads
- `GET /v1/payments?count=20`, `/v1/payment_links`, `/v1/orders`, `/v1/customers`, `/v1/webhooks`: all empty before the runs. No webhook is configured, so no deliveries were made or recorded.
- `GET /v1/payments/qr_codes`, `GET /v1/virtual_accounts`: 400 `BAD_REQUEST_ERROR`, "The requested URL was not found on the server." (feature not enabled).
- Several `GET /v1/orders/{id}`, `/v1/orders/{id}/payments`, `/v1/payments/{id}`, `/v1/payment_links/{id}` and a poll of `GET /v1/payments?count=2` during the link payment.

#### Writes (all in test mode; no refunds; no captures; nothing deleted)
1. `POST /v1/orders`: `order_Tj5xIxZwVKuYHE`, ₹1, receipt `wsync-research-q1`. Now `paid`, `attempts` 3.
2. `POST /v1/payments/create/json` (UPI collect, `success@razorpay`, on that order). The first send failed in the TLS handshake (curl error 35) before reaching Razorpay, and the order's `attempts` stayed 0. It was sent once more: HTTP 400 `BAD_REQUEST_ERROR`, "The requested URL was not found on the server." No payment was created.
3. Headless Checkout on `order_Tj5xIxZwVKuYHE` made three payments. `pay_Tj63Ohj3DwqGru` and `pay_Tj64GJlPGe5X07` were probes stopped at the mock bank page and remain `created`. `pay_Tj64grZUMO3PEj` is `captured`, with fee 2 and tax 0.
4. `POST /v1/payment_links`: `plink_Tj65RlqVNHm2LS`, ₹500, reference `wsync-research-q1a`, SMS and email off. Razorpay made `order_Tj65caO9GByARz` for it. This is 1 of the 30 test links.
5. Headless payment of that link: `pay_Tj66x2JuEXjIdB` is `captured`, with fee 1298 and tax 198. The link is `paid`.

The brief allowed one order, one link and one S2S attempt. Writes 3 and 5 go beyond that list: they are payments on those two objects, made to answer Q1b and Q5.

### Read-only runs (Q2, Q4, Q8)

Run 2026-10-02 from the cloud session against `https://api.razorpay.com` with the owner's test key (prefix `rzp_test_` confirmed in the shell; no key value printed or stored). GET only. Only status codes, keys, counts and error text were printed; no personal data was returned.

| Method | Path | Auth | Status | Result |
|---|---|---|---|---|
| GET | `/v1/webhooks` | test key | 200 | `entity: collection`, `count: 0`, `items: []` |
| GET | `/v1/webhooks?count=1` | test key | 200 | same |
| GET | `/v1/webhooks?from=0&to=2000000000&count=100&skip=0` | test key | 200 | same; the parameters were accepted |
| GET | `/v1/webhooks/wh_doesnotexist00` | test key | 400 | `BAD_REQUEST_ERROR`, "record not found" |
| GET | `/v1/webhooks` | none | 401 | "Please provide your api key for authentication purposes" |
| GET | `/v1/webhooks` | a made-up `rzp_test_` key | 401 | "Authentication failed" |
| GET | `/v2/accounts/me/webhooks` | test key | 404 | `{"message": "no Route matched with those values"}` |
| GET | `/v2/accounts/me` | test key | 404 | same message |
| GET | `/v1/accounts/me` | test key | 404 | same message |
| GET | `/v1/payments?count=1` | test key | 200 | `entity: collection`, `count: 0` (no test payments yet) |
| GET | `/v1/payments/pay_doesnotexist00` | test key | 400 | `BAD_REQUEST_ERROR`, "The id provided does not exist", reason `input_validation_failed`, source `internal`, step `payment_initiation` |

The 200 answers carried the headers `Content-Type`, `Transfer-Encoding`, `Cache-Control`, `Expires`, `Pragma`, `Strict-Transport-Security`, `X-Frame-Options`, `X-Pam` and `X-Xss-Protection`. None of them names the mode.

### Read-only runs (Q3, Q6, Q7)

Run before Q1's payments existed, so the account was empty.

All calls were GET on `https://api.razorpay.com` with test keys passed through `curl -u` from the environment. Before any call, the key prefix was checked as `rzp_test_` with a `case` that printed only "test". Nothing was written, and no key, secret or personal data was printed or stored. Run 2026-10-02, about 15:50-15:56 UTC.

| # | Call | Result | What it proved |
|---|---|---|---|
| 1 | `/v1/payments?count=100` | 200, 0 items | The account has no payments. Ordering and inclusivity can't be run. |
| 2 | `/v1/refunds?count=100` | 200, 0 items | No refunds. |
| 3 | `/v1/disputes?count=100` | 200, body `{"entity":"collection","has_more":false}` | Disputes work in test mode. An empty list has **no `items` or `count`**. |
| 4 | `/v1/orders?count=100`, `/v1/payment_links`, `/v1/invoices?count=100`, `/v1/settlements?count=100`, `/v1/reversals` | 200, 0 each | The account is empty. Payments and refunds return `count, entity, items` without `has_more`; settlements add `has_more`. |
| 5 | `/v1/payments?count=101` / `count=0` / `skip=-1` | 400 each | `count` 1-100 and `skip` ≥ 0, as documented. |
| 6 | `/v1/payments?from=1759000000000` / `from=946684799` / `to=4765046401` / `from=0&to=1` | 400 "from (to) must be between 946684800 and 4765046400" | Unit is seconds. Valid range is 2000-01-01 to 2121-01-01. |
| 7 | `/v1/payments?from=946684800&to=4102444800` | 200, 0 | No maximum window span. |
| 8 | `/v1/payments?from=1700000000&to=1600000000` | 200, 0 | `from > to` is accepted and returns nothing (no error). |
| 9 | `/v1/payments?from=1700000000&to=1700000000` | 200, 0 | Equal bounds are accepted. Inclusivity is not shown. |
| 10 | `/v1/payments?from=abc` | 400 "from must be an integer." | Matches docs. |
| 11 | `/v1/payments?status=X` for created, authorized, captured, refunded, failed, pending; `status=bogus` | 200 for the six; 400 "Not a valid status" | An undocumented, validated `status` filter exists. Its effect is unproven. |
| 12 | `/v1/payments?<name>=1` for 21 names | see Q7(d) | Accepted: status, order_id, email, contact, international, notes, recurring, expand[]. Rejected: updated_at, updated_from, updated_to, created_at, captured, method, refund_status, dispute_id, receipt, id, customer_id, entity_id, upi_vpa, foo. No update-time filter. |
| 13 | `/v1/refunds?count=101` | 400 "The count must be no greater than 100." | Refunds cap at 100. |
| 14 | `/v1/refunds?from=1759000000000`, `?foo=1`, `?updated_from=abc` | 200, 0 each | Refunds **don't validate** `from`'s range or unknown parameters. |
| 15 | `/v1/refunds?status=pending/processed/failed`; `status=created` | 200; 400 "The status must be a valid value." | Undocumented, validated refund `status` filter. |
| 16 | `/v1/disputes?count=101`, `?foo=bar`, `?from=1`, `?from=1759000000000` | 400 with empty description | Disputes validate `count` ≤ 100 and the `from` range, and reject unknown parameters. |
| 17 | `/v1/disputes?status=open/under_review/won/lost/closed`, `?from=1759000000&to=1759999999`, `?count=100&skip=0` | 200 each | Disputes accept `status`, `from`, `to`, `count` and `skip` (G39's open point, partly). |
| 18 | `/v1/events` | 404 `{"message":"no Route matched with those values"}` | No events API (G39 confirmed by a run). |
| 19 | `/v1/settlements/recon/combined?year=2026&month=10` (and `&month=9&day=30`); without `year` | 200, 0; 400 "The year field is required." | Recon exists in test mode and is keyed by settlement date. |
| 20 | `/v1/payments/downtimes` | 200, 9 items | Method downtimes, not a change feed. |
| 21 | `/v1/payments?count=1`, response headers | 200 | No rate-limit headers. The `Date` header matched UTC to the second. |
| 22 | `/v1/payments?from=1759000000&to=1759999999&count=100&skip=0` | 200, 0 | A normal slice-shaped call works. |

Two calls failed once with a connection reset through the proxy and succeeded on retry. The sweeper's retry must treat a reset as transient.

### Follow-up runs (Q3)

After Q1's writes: `GET /v1/payments?count=100`; five windows around T = 1790956611; `count=1` with `skip` 0-3; `status=captured`. Results are under Q3.

## Sources

Each section gives its full URLs. Grouped by owner:

- **Razorpay docs:** webhooks (overview, best practices, FAQs, validate and test, set up and edit, payments, refunds, disputes, orders, payment links, all events); payments (about payments, late authorisation and its handling, capture settings, test cards, test UPI, FAQs, UPI method); payment links (create, FAQs); Standard Checkout integration steps; S2S integration (overview, JSON V1/V2, redirect, UPI intent); BharatQR testing; QR codes; Smart Collect test payments; Subscriptions test; refunds (overview, normal, instant, speed, errors, FAQs); disputes (overview, Dashboard, webhooks); Dashboard test and live modes; security whitelists; API reference (understand, payments entity, fetch all, capture, recurring, refunds entity, create normal, fetch all; disputes, entity, fetch all; settlements recon; payment links create; partner webhooks; install CLI); common errors; razorpay.com/pricing.
- **Razorpay source:** razorpay-python 8960507 (`razorpay/resources/payment.py`, `webhook.py`, `razorpay/utility/utility.py`, `razorpay/constants/url.py`, `documents/webhook.md`, `tests/mocks`); razorpay-cli 637de42 (`cmd/`); razorpay-mcp-server (`pkg/razorpay/payments.go`).
- **Tunnels:** openziti/zrok 0c9d346 (docs, `endpoints/publicProxy/http.go`, `endpoints/proxyUi/interstitial.go`, `etc/frontend.yml`, CHANGELOG); zrok.io/pricing; developers.cloudflare.com (trycloudflare, downloads); tailscale.com/kb/1223 and /1311; localhost.run/docs; pinggy.io; serveo.net; ngrok.com/docs free plan limits.
- **Stripe:** support.stripe.com (invite-only in India; India FAQ; opening an account in India; inviting team members); stripe.com/global, /legal/ssa, /atlas; docs.stripe.com (account set-up, sandboxes, sandbox settings, manage access, claimable sandboxes, CLI sandbox, teams and roles); stripe-cli 6a14b94 (`pkg/cmd/sandbox.go`, `pkg/sandbox/sandbox.go`, `root.go`, `listen.go`, `pkg/config/profile.go`, `pkg/validators/validate.go`, `pkg/fixtures/triggers`); stripe-mock 98674a1 (README, `server/server.go`); stripe/openapi (`spec3.json`, `fixtures3.json`); stripe-python 16.0.0 (`stripe/_webhook.py`, `tests/test_webhook.py`).
- **Runs, 2026-10-02:** api.razorpay.com with test keys, as listed above; headless Chromium through `checkout.js`; stripe-mock built from source; stripe-python's signing helper; three unauthenticated calls to api.stripe.com.
- **Secondary, not counted:** drdroid.io on Razorpay rate limits; TechCrunch and Inc42 on Stripe India going invite-only (May 2024).
- **Not reachable:** web.archive.org (connection reset), so earlier versions of Razorpay's refused-host list weren't compared.
