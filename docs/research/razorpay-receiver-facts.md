# Research: Razorpay facts for the receiver (build step P1)

Researched 2026-10-02 against primary sources, for five questions raised while building the webhook receiver (P1). This note does not repeat what [razorpay-first.md](razorpay-first.md) (Q2, Q8) and [gaps-google-python-razorpay.md](gaps-google-python-razorpay.md) (G38) already settle. It cites them instead, and corrects one of their findings (see Q2).

Primary means razorpay.com/docs, read as the Markdown copies Razorpay serves for each page (`https://razorpay.com/docs/<path>.md`), and Razorpay's own full-text dump of the same docs (`https://razorpay-881012b3.mintlify.site/llms-full.txt`, which every docs page names as its index). Each claim below cites the razorpay.com/docs page it came from. Also used: the source of razorpay-python (commit 8960507, 2026-03-09). The sample payloads were parsed with a script: 47 samples across the payments, refunds, orders, payment-links and disputes webhook pages. A few read-only GETs went to api.razorpay.com with the owner's **test** key. The prefix was checked as `rzp_test_` first, and no key value was printed or stored (see "API runs"). No secondary source was used. GitHub code search was blocked in this session, so Razorpay repos other than razorpay-python were not searched.

| Q | Question | Verdict | One-line finding |
|---|---|---|---|
| Q1 | Top-level `account_id` | **confirmed on samples; format documented for partners only** | Every one of the 47 samples for the events we handle or store has `account_id` = `acc_` + 14 characters. The Dashboard shows the bare id (no `acc_`) top right. `GET /v1/webhooks` returns it as `owner_id` once a webhook exists. No payment, refund or order entity carries it. |
| Q2 | `x-razorpay-event-id` on every delivery | **partly documented; corrects razorpay-first Q2** | It is "unique per event" and "stays the same across the original delivery and every replay". It is not stated to be present on every delivery or explicitly the same on automatic retries. A new page documents a **self-service Dashboard replay** (India only). |
| Q3 | Secret rotation | **documented, with a conflict** | Retries of events created before the change are signed with the **old** secret. Dashboard replays are signed with the **current** secret. One secret per webhook. |
| Q4 | Top-level `created_at` | **not documented beyond one phrase** | Unix seconds. The docs call it the "event creation timestamp" only in passing. It differs from the entity's `created_at`. The samples are inconsistent, and one (`refund.speed_changed`) has no top-level `created_at`. |
| Q5 | Order of `contains` | **samples only** | payment.\* `["payment"]`; refund.\* `["refund","payment"]`; order.paid `["payment","order"]`; payment_link.paid/partially_paid `["payment_link","order","payment"]`; cancelled/expired `["payment_link"]`; dispute.\* `["payment","dispute"]`. No page states an order. |

## Q1: the body's top-level `account_id`

**Question.** Is `account_id` present on every event type we handle or store? What is its format? Where can an owner see it? Does a payment entity carry it?

**Verdict: present on every sample, and its format is clear from partner docs. Not stated for merchants in words.** A receiver can read it from every handled event. It should still treat a missing value as malformed rather than crash.

**What the sources say.**

*Present on every sample.* [razorpay-first.md](razorpay-first.md) Q8 already found `account_id` (`acc_…`) on all 31 samples on the payments page. This pass parsed the samples on all five pages that cover our events. Every one carries a top-level `account_id` of the form `acc_` + 14 alphanumerics:
- payments: `payment.authorized`, `.captured` and `.failed` × 4 methods each; also `payment.pending` and the `payment.downtime.*` samples (https://razorpay.com/docs/webhooks/payments/)
- refunds: `refund.created`, `.processed`, `.failed` and `.speed_changed` (https://razorpay.com/docs/webhooks/refunds/)
- orders: `order.paid` × 4 (https://razorpay.com/docs/webhooks/orders/)
- payment links: `payment_link.paid` × 2, `.partially_paid`, `.cancelled` × 2 and `.expired` × 2 (https://razorpay.com/docs/webhooks/payment-links/)
- disputes: all six `payment.dispute.*` events (https://razorpay.com/docs/webhooks/disputes/)

Samples are illustrations, not a schema. **No page describes the envelope fields in a table or says that `account_id` is always present.** Not documented.

*Meaning and format.*
- The partner OAuth webhook sample annotates the field itself: `"account_id": "acc_Dhk2qDbmu6FwZH", // merchant account id` (https://razorpay.com/docs/webhooks/partners/oauth/).
- The OAuth token response returns `razorpay_account_id: "acc_Dhk2qDbmu6FwZH"`, which "Identifies the sub-merchant ID who granted the authorisation" (https://razorpay.com/docs/partners/technology-partners/onboard-businesses/integrate-oauth/integration-steps/).
- The onboarding SDK page says an account id `acc_HQVlm3bnPmccC0` "must be entered without the 'acc\_'", as `HQVlm3bnPmccC0`, where a "sub-merchant id" is wanted (https://razorpay.com/docs/partners/technology-partners/onboard-businesses/onboarding-sdk/).
- The partner webhooks "Fetch All" sample calls `GET /v2/accounts/acc_H3kYHQ635sBwXG/webhooks`. Each returned webhook has `"owner_id": "H3kYHQ635sBwXG"`, described as "The unique identifier generated by Razorpay for the sub-merchant who will receive the webhooks … it will be `account_id` passed in the API URL" (https://razorpay.com/docs/api/partners/webhooks/fetch-all/).
- A RazorpayX response field `merchant_id` gives the example `Pup4LDLiA1DssX`, 14 characters with no prefix (https://razorpay.com/docs/api/x/payout-composite/create/phone-number/).

So `account_id` = `acc_` + the 14-character Merchant ID (MID). That is stated for partners and sub-merchants. For a direct merchant it is an inference from the same id scheme, with no page saying otherwise.

*Where the owner can see it.*
- **Dashboard.** "You can find your Account ID/Merchant ID at the top-right corner of your Dashboard in alpha-numeric characters" (https://razorpay.com/docs/payments/dashboard/faqs/). "Alpha-numeric" implies the bare MID, without `acc_`; the screenshot was not available in the Markdown copy. The Account & Settings, Account Details, Business Details and Activation Details pages don't mention it (https://razorpay.com/docs/payments/dashboard/account-settings/ and its sub-pages).
- **API with key-id auth.** No documented merchant endpoint returns the merchant's own id. The one route that works is `GET /v1/webhooks`, which is undocumented for merchants but works with a merchant test key ([razorpay-first.md](razorpay-first.md) Q8; razorpay-python `razorpay/resources/webhook.py` calls `/v1/webhooks` when `account_id` is `None`). By the partner entity above, each webhook item should carry `owner_id` = the MID. Our test account has no webhook yet (`count: 0`), so this is **not yet seen first-hand**. Guessed self endpoints all gave 404 (see "API runs").
- **Webhook log.** The Dashboard's webhook log shows each delivery's Request ([razorpay-first.md](razorpay-first.md) Q2a), and so its `account_id`.

*Not on fetched entities.* The payments entity, fetch-by-id and fetch-all pages, and the refunds, orders and disputes entity pages, list no `account_id`, `merchant_id` or `acc_` field (https://razorpay.com/docs/api/payments/entity/, /api/payments/fetch-with-id/, /api/payments/fetch-all-payments/, /api/refunds/entity/, /api/orders/entity/, /api/disputes/entity/). Our runs agree. `GET /v1/payments?count=1`, `GET /v1/payments/{id}` (also with `expand[]=transaction`), `GET /v1/refunds?count=1` and `GET /v1/orders?count=1` returned no key containing "account" or "merchant" and no `acc_` value.

*Test versus live.* No page says whether the test-mode and live-mode `account_id` match. Not documented; [razorpay-first.md](razorpay-first.md) Q8 left the same point open.

**What the design should change**
- [sources.md](../design/sources.md) "Razorpay: the differences": add an **Account** row. The account is the body's top-level `account_id` (`acc_…`), as received. A missing or non-string value is `malformed`. The fetch can't confirm it, because no entity carries it.
- `doctor`/`check_setup()` (Q8's best-effort webhook check): when `GET /v1/webhooks` lists our webhook, compare `"acc_" + owner_id` with the configured account, if one is configured. Warn on a mismatch. Never fail on it, since the endpoint is undocumented for merchants.
- The quickstart: "Account ID is the id at the top right of the Dashboard. Prefix it with `acc_`", or learn it from the first delivery.

**Spike (laptop, with the test webhook from razorpay-first Q4)**
- `GET /v1/webhooks` once a webhook exists. Check that `owner_id` is present and that `"acc_" + owner_id` equals the delivery's `account_id`. Compare both with the Dashboard's top-right id.

## Q2: is `x-razorpay-event-id` on every delivery, and the same across retries?

**Question.** Is the header sent on every delivery, including retries and the Dashboard's resend or test deliveries? Is it the same id across retries?

**Verdict: the same across an event's deliveries, as far as documented. Presence on every delivery is not stated.** Also, **Razorpay now documents a self-service Dashboard replay.** This contradicts [razorpay-first.md](razorpay-first.md) Q2 ("No Dashboard or API resend").

**What the sources say.**

*Uniqueness.* "The value for this header is unique per event and can help you determine the duplicity of a webhook event" (https://razorpay.com/docs/webhooks/best-practices/, https://razorpay.com/docs/webhooks/faqs/; the same wording is on https://razorpay.com/docs/webhooks/validate-test/). Already in G38 ([gaps-google-python-razorpay.md](gaps-google-python-razorpay.md)).

*Replays: same id, said outright.* A page titled "Replay Webhook Events" says: "Use the `x-razorpay-event-id` header to identify duplicates. The value of this header is unique per event and stays the same across the original delivery and every replay of it" (https://razorpay.com/docs/webhooks/replay-events/).

*Automatic retries.* No page says in words that a retry carries the same id. A retry is a resend of the same event ("if we do not receive a successful response from your server, we resend the webhook", https://razorpay.com/docs/webhooks/best-practices/). The id is "unique per event", so the same id follows by reading. This stays as G38 left it: implied, not stated.

*Every delivery.* No page says the header is present on every delivery, or that it is ever absent. The only documented headers are `X-Razorpay-Signature` and `x-razorpay-event-id` (https://razorpay.com/docs/webhooks/validate-test/). Not documented.

*Test deliveries.* No page documents a "send test event" button. Test events are real events from test-mode transactions, and "the payload structure remains the same in the **Live** and **Test** modes" (https://razorpay.com/docs/webhooks/validate-test/; already in [razorpay-first.md](razorpay-first.md) Q2d). Headers on test-mode deliveries are not described separately. Not documented.

*The Dashboard replay (new; corrects razorpay-first Q2a).* Quotes from https://razorpay.com/docs/webhooks/replay-events/:
- "You can replay these missed events yourself from the Razorpay Dashboard, without raising a support ticket." It is under **Developers → Webhooks → Replay events**. You either replay all events in a date range, or pick events by entity (Payment, Order, Invoice, Subscription, Refund, QR Code), up to 500 ids per request.
- "Only **unacknowledged** events are replayed. Events your server already acknowledged with a 2xx response are not sent again."
- Events up to "**7 days old**". Beyond that, contact Support.
- "a maximum of **5 replays in a 24-hour window**."
- A disabled webhook URL "is **enabled automatically** when you replay the events."
- "Replayed events are sent to your endpoint exactly as they were originally generated and are signed with your current webhook secret."
- "Replayed events are queued separately from live webhook traffic."
- The page says "Available in 🇮🇳 India". It does not say whether replay works in test mode. Not documented.

Provenance: the page is served at `https://razorpay.com/docs/webhooks/replay-events.md` and appears in Razorpay's full docs dump. It is not in `https://razorpay.com/docs/llms.txt`. The HTML URL redirected this US-located session to `/docs/us/…`, which gives 404 because the page is India-only. The FAQ still describes only the older support-ticket replay ("not older than 15 days", "bulk replaying … is not possible", https://razorpay.com/docs/webhooks/faqs/). So the replay page is newer than the FAQ and the screenshots razorpay-first relied on.

**What the design should change**
- [razorpay-first.md](razorpay-first.md) Q2 and [research-gaps.md](../design/research-gaps.md): "no Dashboard resend" is out of date. A Dashboard replay exists, for unacknowledged events only.
- [showcase.md](../design/showcase.md) duplicate scene: a third option. The demo fault switch answers 500 to the first N attempts, so the event stays unacknowledged. Replay it from the Dashboard, if the laptop spike shows replay works in test mode. Note the limit of 5 replays a day.
- [receiver.md](../design/receiver.md): keep deduping on both the event id and the signed body's hash ([Q86](../design/grilling-log.md)). A missing `x-razorpay-event-id` should still be handled as the receiver table already plans: undocumented either way, so don't assume it.
- [edge-cases.md](../design/edge-cases.md): a replay can arrive up to 7 days after the event, carrying the original body (and so the original `created_at`). The receiver has no age check, which is correct. Keep it that way.

**Spike (laptop)**
- With the fault switch at N = 4, log every attempt's `x-razorpay-event-id` and body SHA-256 (already planned as the G38 instrument). Then press **Replay events** once for that payment in the test Dashboard. If the button exists in test mode, log the same two values. This settles "same id on retries" first-hand, and whether test mode can replay.

## Q3: webhook secret rotation

**Question.** When the owner edits a webhook's secret, are pending retries of earlier events signed with the old or the new secret? Is more than one secret per webhook supported?

**Verdict: retries keep the old secret; replays use the current one; one secret per webhook.**

**What the sources say.**

*Retries: the old secret.* "If you have changed your webhook secret, remember to use the old secret for webhook signature validation while retrying older requests. The new secret can only be used for all events generated after the secret is updated" (https://razorpay.com/docs/webhooks/faqs/). The first sentence appears on about 20 webhook pages, followed by "Using the new secret will lead to a signature mismatch" (for example https://razorpay.com/docs/webhooks/validate-test/, /webhooks/payments/, /webhooks/refunds/, /webhooks/disputes/). Reading: a pending retry of an event created before the change still carries the signature made with the **old** secret, for up to the 24-hour retry window ("24 hours after event creation timestamp", https://razorpay.com/docs/webhooks/best-practices/). The sentence is ambiguous about who is "retrying". The FAQ's second sentence ties the secret to the event's creation time, which supports this reading.

*Replays: the current secret.* "Replayed events are … signed with your current webhook secret" (https://razorpay.com/docs/webhooks/replay-events/). This conflicts with the old-secret rule above, unless that rule covers automatic retries only. Read together: automatic retries use the secret from event creation, and Dashboard replays re-sign the same bytes with today's secret. Support-ticket replays need "signature validation … using the same signature during the occurrence of this event" ([razorpay-first.md](razorpay-first.md) Q2b, https://razorpay.com/docs/webhooks/faqs/).

*One secret per webhook.* The Dashboard edit form has a single **Secret** field (https://razorpay.com/docs/webhooks/setup-edit-payments/). The webhook API takes `secret` as one `string`, and the entity reports only `secret_exists` (boolean) (https://razorpay.com/docs/api/partners/webhooks/create/, /api/partners/webhooks/entity/). No page mentions a second or rolling secret, or a signature header with more than one value. razorpay-python's `verify_webhook_signature(body, signature, secret)` takes one secret and computes one HMAC (`razorpay/utility/utility.py` L56-68). Up to 30 webhook URLs are allowed per account (https://razorpay.com/docs/webhooks/faqs/). Each URL has its own secret, so that is not a dual-secret mechanism for one URL.

**What the design should change**
- [edge-cases.md](../design/edge-cases.md) A5 and A6 already accept "any signature matches any configured secret". For Razorpay, the runbook's rotation steps: (1) add the new secret to our config next to the old one; (2) change it in the Dashboard; (3) keep the old one for at least **24 hours**, the retry window. Note that a Dashboard replay done after the change is signed with the **new** secret even for old events. Then remove the old one, using `verify`'s "which secret matched" to confirm.
- The receiver's dedupe on the body hash still holds across a rotation: the replay page says bodies are sent "exactly as they were originally generated", so only the signature changes.

**Spike.** Optional: rotate the test webhook's secret while a fault-switch event is still retrying, and record which secret each later attempt verifies with.

## Q4: the body's top-level `created_at`

**Question.** Is the top-level `created_at` the event's creation time in Unix seconds? Does it differ from the entity's `created_at`?

**Verdict: Unix seconds, called the event's creation time only in passing. It is a different value from the entity's `created_at`.** No page defines the field.

**What the sources say.**
- No page has a field table for the event envelope. The only wording is in the retry rule: "for 24 hours after event creation timestamp" (https://razorpay.com/docs/webhooks/best-practices/; "24 hours after event creation timestamp", https://razorpay.com/docs/webhooks/faqs/). That names an event creation time but not the field. Not documented as a definition.
- Every sample value has 10 digits (for example `1567674606`), so it is Unix **seconds**. Entity `created_at` is documented as "Timestamp, in UNIX format" ([razorpay-first.md](razorpay-first.md) Q3; https://razorpay.com/docs/api/payments/entity/).
- Differs from the entity's time. From the parsed samples (pages as in Q1):
  - `payment.captured` netbanking: event 1567674606, payment 1567674599 (7 s later).
  - `order.paid` netbanking: event 1567674606, payment 1567674599, order 1567674581.
  - `payment.dispute.lost`: event 1589908238, dispute 1589907977, payment 1581525157.
  - `refund.created`, `.processed` and `.failed`: event = refund `created_at` (1597734071), payment older.
- **The samples are inconsistent, so don't build on their values.**
  - The three card samples (`payment.authorized`, `.captured`, `.failed`) have event 1691735748 (2023) on a payment created 1567674797 (2019).
  - The `payment_link.paid` samples have the event time equal to the link's `created_at` and **earlier** than the payment's (1749618314 versus 1749618371).
  - The `refund.speed_changed` sample has **no top-level `created_at`**. It sits inside `payload` instead (`payload.created_at`), alongside `refund` and `payment` (https://razorpay.com/docs/webhooks/refunds/). That looks like a docs error, but a strict parser would reject the sample.
- A replay sends the body "exactly as … originally generated" (https://razorpay.com/docs/webhooks/replay-events/), so a replayed event keeps its original `created_at`.

**What the design should change**
- [data-model.md](../design/data-model.md) `source_created_at` for Razorpay events: the top-level `created_at`, documented only as the "event creation timestamp". Store null if it is missing, rather than rejecting the event. A fixture built from the `refund.speed_changed` sample must pass.
- [sources.md](../design/sources.md) Razorpay "Fingerprint: `event`, the entity id, `created_at`": say which `created_at`. The entity's is the safer choice, because it is always on the entity per the entity pages. But a payment's `created_at` never changes, so `payment.authorized` and `payment.captured` already differ by `event`. If the top-level one is used, a missing value must not crash the fingerprint.
- Never use the top-level `created_at` as the payment's creation date. The sheet's date stays the fetched payment's `created_at`, as data-model.md already says.

## Q5: the `contains` array

**Question.** What order does `contains` list entities in, per event type?

**Verdict: from the samples only.** The one page that describes the array says "The `contains` array names the entity present in the `payload` object" (Bill Payments, https://razorpay.com/docs/webhooks/bbps/). No page states an order or promises one.

| Event | `contains` in every sample | `payload` keys in the sample | Page |
|---|---|---|---|
| `payment.authorized`, `.captured`, `.failed` (and `.pending`) | `["payment"]` | `payment` | https://razorpay.com/docs/webhooks/payments/ |
| `refund.created`, `.processed`, `.failed`, `.speed_changed` | `["refund", "payment"]` | `refund`, `payment` (`speed_changed` also has a stray `created_at`) | https://razorpay.com/docs/webhooks/refunds/ |
| `order.paid` | `["payment", "order"]` | `payment`, `order` | https://razorpay.com/docs/webhooks/orders/ |
| `payment_link.paid`, `.partially_paid` | `["payment_link", "order", "payment"]` | `order`, `payment`, `payment_link`, in a **different order** from `contains` | https://razorpay.com/docs/webhooks/payment-links/ |
| `payment_link.cancelled`, `.expired` | `["payment_link"]` | `payment_link` | https://razorpay.com/docs/webhooks/payment-links/ |
| `payment.dispute.*` (all six) | `["payment", "dispute"]` | `payment`, `dispute` | https://razorpay.com/docs/webhooks/disputes/ |

For comparison, the partner OAuth event `account.app.authorization_revoked` has `"contains": []` and no `payload` (https://razorpay.com/docs/webhooks/partners/oauth/).

**What the design should change**
- [sources.md](../design/sources.md) "Payment key": read entities **by name** (`payload.payment.entity`, `payload.refund.entity`), never by position in `contains`. The payment-link samples show that `contains` order and `payload` key order differ. The receiver should not validate `contains` beyond "is a list". An event whose needed `payload.<name>.entity` is missing is `malformed` for handled types, and stored as ignored for the others.

## API runs

Run 2026-10-02, about 17:05 UTC, from the cloud session against `https://api.razorpay.com`. The key prefix was confirmed as `rzp_test_` in the shell before each batch. GET only; nothing was written. Only status codes, key names, counts and id shapes were printed. No key, secret or personal data was printed or stored.

| Method | Path | Auth | Status | Result |
|---|---|---|---|---|
| GET | `/v1/payments?count=1` | test key | 200 | Item keys include no `account_id` or `merchant_id`; no `acc_` value |
| GET | `/v1/payments/{pay_…}` | test key | 200 | Same: no account field |
| GET | `/v1/payments/{pay_…}?expand[]=transaction` | test key | 200 | Same, plus `transaction`; no account field |
| GET | `/v1/refunds?count=1` | test key | 200 | No account field |
| GET | `/v1/orders?count=1` | test key | 200 | Item keys: amount, amount_due, amount_paid, attempts, created_at, currency, entity, id, notes, offer_id, receipt, status |
| GET | `/v1/webhooks` | test key | 200 | Still an empty collection (no webhook yet), so `owner_id` could not be seen |
| GET | `/v1/merchants/me`, `/v1/merchant`, `/v1/merchants`, `/v1/accounts/me`, `/v2/accounts`, `/v1/users/me` | test key | 404 | `{"message": …}` (no route) |
| GET | `/v1/preferences?key_id=<test key id>` | none | 200 | Checkout's undocumented preferences. `merchant` holds only `lending` and `mcc`; no merchant or account id anywhere |

## Sources

- **Razorpay docs** (each read as `<url>.md`):
  - Webhooks: https://razorpay.com/docs/webhooks/payments/, /webhooks/refunds/, /webhooks/orders/, /webhooks/payment-links/, /webhooks/disputes/, /webhooks/best-practices/, /webhooks/validate-test/, /webhooks/faqs/, /webhooks/setup-edit-payments/, /webhooks/replay-events/, /webhooks/bbps/, /webhooks/partners/oauth/, https://razorpay.com/docs/webhooks/
  - API reference: /api/payments/entity/, /api/payments/fetch-with-id/, /api/payments/fetch-all-payments/, /api/refunds/entity/, /api/orders/entity/, /api/disputes/entity/, /api/partners/webhooks/create/, /api/partners/webhooks/entity/, /api/partners/webhooks/fetch-all/, /api/x/payout-composite/create/phone-number/
  - Dashboard: /payments/dashboard/faqs/, /payments/dashboard/account-settings/ (and account-details, business-details, activation-details, webhooks), /payments/mobile-app/settings/
  - Partners: /partners/technology-partners/onboard-businesses/integrate-oauth/integration-steps/, /partners/technology-partners/onboard-businesses/onboarding-sdk/
  - Index: https://razorpay.com/docs/llms.txt and Razorpay's full-text dump https://razorpay-881012b3.mintlify.site/llms-full.txt (used to search; claims cite the razorpay.com page).
- **Razorpay source:** razorpay-python 8960507 (`razorpay/utility/utility.py`, `razorpay/resources/webhook.py`, `razorpay/resources/account.py`, `tests/mocks`).
- **Earlier notes cited:** [razorpay-first.md](razorpay-first.md) (Q2, Q3, Q8), [gaps-google-python-razorpay.md](gaps-google-python-razorpay.md) (G38).
- **Runs, 2026-10-02:** api.razorpay.com with the test key, as listed above.
- **Not reachable:** the HTML page for /webhooks/replay-events/ (geo-redirected to the US site, where the India-only page is 404); its Markdown copy was read instead. GitHub code search (blocked by the session's GitHub proxy).
