# Research: Razorpay facts for the fetch (build step P2)

Researched 2026-10-02 against primary sources, for nine questions raised while building P2. P2 fetches `GET /v1/payments/{id}` (and maybe its refunds) and maps one payment to a ledger row. This note does not repeat what [razorpay-first.md](razorpay-first.md) (Q3, Q5, Q6, Q7), [gaps-google-python-razorpay.md](gaps-google-python-razorpay.md) (G39, G41) and [razorpay-receiver-facts.md](razorpay-receiver-facts.md) (Q4) already settle. It cites them instead.

Primary means razorpay.com/docs, read as the Markdown copies Razorpay serves (`https://razorpay.com/docs/<path>.md`), searched through Razorpay's own full-text dump (`https://razorpay-881012b3.mintlify.site/llms-full.txt`). Every claim cites the razorpay.com page, and the key quotes were re-checked on the razorpay.com copy. Also used: the source of razorpay-python (commit 8960507, 2026-03-09, version 2.0.1) and live test-mode responses. Calls went to api.razorpay.com with the owner's **test** key. The prefix was checked as `rzp_test_` first, and only GETs were made. No key, email, phone number or bank reference was printed or stored: the helper redacted those fields before printing. No order or payment was created, because every route that completes a payment needs a browser ([razorpay-first.md](razorpay-first.md) Q1). The account already held seven test payments and two refunds, made earlier on 2 October. No secondary source was used.

| Q | Question | Verdict | One-line finding |
|---|---|---|---|
| Q1 | Fetch errors | **settled by runs** | Any unknown id gives **400** `BAD_REQUEST_ERROR` "The id provided does not exist" on `/v1/payments/{id}`: well-formed, malformed, wrong prefix, or (per the docs) the other mode. **`/v1/payments/{id}/refunds` answers 200 with an empty list for a well-formed unknown id**, and 404 `{"message": …}` for a malformed one. |
| Q2 | `amount_refunded` with pending or failed refunds | **not documented** | No page says whether pending refunds count, or what a failed refund does to these fields. The refund list says pending versus processed. `expand[]=refunds` brings it in the same call. |
| Q3 | Authorised, never captured | **partly documented** | Auto-refunded after the capture timeout (at most 3 days), at normal speed. The resulting `status`, `captured`, `amount_refunded` and `refund_status` are **not documented**. Spike S-Q7a stays open. |
| Q4 | Method details | **documented, one docs conflict** | Always on the entity: `method`, `card_id`, `bank`, `wallet`, `vpa`, `email`, `contact`. The card brand (`card.network`) needs `expand[]=card` or `GET /v1/payments/{id}/card`. `upi` comes back inline. `expand[]=emi` adds `emi` (and `emi_plan`). |
| Q5 | Partial capture | **documented: no** | "Capture amount must be equal to the amount authorized." `amount` never changes. There is no separate authorised-amount field. `amount_captured` is documented, but it is null or absent in practice. |
| Q6 | Settlement and international | **undocumented route found** | The payment entity has no settlement field. `expand[]=transaction` (documented only in an error message) adds `credit` (= net), `fee`, `tax`, `settled`, `settled_at` and `settlement_id`. Non-INR payments carry `base_amount`/`base_currency` (INR). Fees and settlements are calculated on those, so **net is not `amount − fee` across currencies**. |
| Q7 | Customer name | **documented: none** | A payment has `email` and `contact` only. The only "name" is the cardholder's, inside `card`. `description` exists and is often null. |
| Q8 | Rate limits, timeouts, 5xx/429 | **settled before (G54), plus SDK facts** | No numbers are published. 429's body says `BAD_REQUEST_ERROR`. The 5xx bodies are documented on the RazorpayX errors page only. razorpay-python has **no default timeout**, throws away the HTTP status, and fails on non-JSON bodies. |
| Q9 | `created_at`, `captured_at` | **documented** | `created_at` is Unix seconds (UTC by definition). No page and no response has `captured_at`. The transaction's `created_at` (undocumented) is the nearest stand-in. |

## Q1: what the fetch returns for ids that don't exist

**Question.** What exact status and `error` do `GET /v1/payments/{id}` and `GET /v1/payments/{id}/refunds` return for a missing id, a malformed id, and an id from the other mode?

**Verdict: settled by runs for the first two cases. The other mode is covered by the docs only.** The two endpoints disagree, and the refunds endpoint disagrees with its own docs.

**What the sources say.**

*`GET /v1/payments/{id}`, seen live (see "API runs").* All of these return **HTTP 400** with this body, byte for byte apart from key order:

```json
{"error": {"code": "BAD_REQUEST_ERROR", "description": "The id provided does not exist",
           "source": "internal", "step": "payment_initiation", "reason": "input_validation_failed", "metadata": {}}}
```

The ids tried: a well-formed unknown id `pay_00000000000000`; a real id with one character changed; `abc`; `pay_`; a real id without `pay_`; a real id with a 15th character; an `order_…` id; a `rfnd_…` id; and `%20`. One exception: an id with an encoded slash (`pay_abc%2Fdef`) got an **HTML** 404 page from the edge, with no JSON. The prefix is not case-sensitive: `PAY_…` fetched the payment, and the response gave the id back in lower case.

*The docs.* The fetch page lists "The id provided does not exist" as a 400 for an incorrect `payment_id`. Its sample has `"source": "business"`, where the live answer says `internal` (https://razorpay.com/docs/api/payments/fetch-with-id/). The common-errors page names three causes for a related message, "The id provided does not exist or access is unauthorised": the id doesn't exist; "The entity belongs to a different account"; and "A test mode ID is used in live mode or vice versa" (https://razorpay.com/docs/errors/common/). So **another mode's id is not told apart from a missing one**, and the description text varies between pages. A live payment id could not be tried, because ids carry no mode ([razorpay-first.md](razorpay-first.md) Q8). The run with `pay_00000000000000` is the closest stand-in.

*`GET /v1/payments/{id}/refunds`, seen live.*

| Path id | Status | Body |
|---|---|---|
| a well-formed unknown id (`pay_` + 14 characters, including a real id with one character changed) | **200** | `{"entity": "collection", "count": 0, "items": []}`, the same as a real payment with no refunds |
| `abc`, `pay_`, `pay_abc`, a real id with a 15th character | 404 | `{"message": "no Route matched with those values"}` (no `error` object) |
| a real id without `pay_`, an `order_…` id | 400 | `BAD_REQUEST_ERROR`, "<id> is not a valid id", `source: business` |
| a real payment, `count=101` | 400 | "The count must be no greater than 100." |

The docs say a missing `payment_id` on this route gives 400 "The id provided does not exist" (https://razorpay.com/docs/api/refunds/fetch-multiple-refund-payment/). The run did **not** confirm that: an unknown well-formed id gives 200 and an empty list. The list carries no `has_more`, as on the other lists ([razorpay-first.md](razorpay-first.md) Q3).

*`GET /v1/payments/{id}/card`* gives 400 "Payment was not done using card" (`source: NA`) for a netbanking payment, and the usual "does not exist" 400 for an unknown id. That matches its page (https://razorpay.com/docs/api/payments/fetch-card-details-payment/).

**What the design should change**
- [sources.md](../design/sources.md) "Mode" row: keep "400 'The id provided does not exist' → `not_found`, naming a possible mode mix-up". Match on **HTTP 400 + `error.code == BAD_REQUEST_ERROR` + description starting "The id provided does not exist"**. Don't match on `source`, because the docs and the live answer differ.
- Always fetch the payment first. **Never take an empty refund list as proof that the payment exists.**
- Classify these by status as well: a 404 whose body has no `error` object (a route miss, so a bug in our id or path), and a non-JSON body. Neither is retryable. The ids come from signed events and the API's own lists, so a malformed id is a bug or a forgery, and it should go dead with a clear reason.

## Q2: do `amount_refunded` and `refund_status` count pending refunds?

**Question.** Do `amount_refunded` and `refund_status` count `pending` refunds, or only `processed` ones? What happens when a refund ends `failed`? Is the refund list needed?

**Verdict: not documented either way.** The refund list (or `expand[]=refunds`) is the only documented source of pending versus processed.

**What the sources say.**
- `amount_refunded` is "The amount refunded in currency subunits", and `refund_status` is `null`, `partial` or `full` (https://razorpay.com/docs/api/payments/entity/). No page says which refund states count. A search of the docs dump for prose about either field found only those field descriptions, repeated on about 40 pages.
- A refund is `pending` while Razorpay "is attempting to process the refund", and `processed` is "the final state" (https://razorpay.com/docs/api/refunds/entity/; [razorpay-first.md](razorpay-first.md) Q6). Refunds can stay `pending` for days: "a refund status can remain in a 'pending' state, particularly when the refund cannot be processed instantly" (https://razorpay.com/docs/payments/refunds/faqs/).
- Indirect hint: the batch-refund page gives the error "The refund amount provided is greater than the unrefunded amount" (https://razorpay.com/docs/payments/refunds/batch/), and the create-refund API errors include "The payment has been fully refunded already" (https://razorpay.com/docs/api/refunds/create-normal/). That suggests a refund counts against the payment once it is created. It is an inference, not a statement.
- *Failed refunds.* No page says whether a `failed` refund is taken back out of `amount_refunded`. The `refund.failed` webhook sample embeds a payment with `amount_refunded` 190000 and `refund_status: partial`. That is the same payment snapshot as in the `created`, `processed` and `speed_changed` samples (https://razorpay.com/docs/webhooks/refunds/), so the samples settle nothing. A failed instant refund falls back to normal speed rather than ending `failed`, and a normal refund of a payment over 6 months old is refused at creation ([razorpay-first.md](razorpay-first.md) Q6). So `failed` is rare.
- *Fees are kept.* "fees and taxes charged for a captured payment are not reversed" on a refund (https://razorpay.com/docs/payments/refunds/faqs/; also https://razorpay.com/docs/payments/refunds/normal/). So `fee` and `tax` don't change when a refund is made.
- *Live.* The account's two refunds, ₹100 each on one ₹500 payment (one `speed: normal`, one `optimum`), were both `processed` when read. The payment showed `amount_refunded` 20000 and `refund_status: partial`, which is consistent with either reading. A pending refund could not be seen: creating refunds was outside this run's allowed writes.
- *One call is enough.* `GET /v1/payments/{id}?expand[]=refunds` returned the payment with `refunds: {entity: collection, count, items}`. The items were identical to `GET /v1/payments/{id}/refunds`. The expand is accepted on a single fetch: the API's validation message lists `refunds` among the allowed expands. But no page documents it for `GET /v1/payments/{id}`, and whether it caps the list (the plain route defaults to a page of 10, at most 100) is unknown.

**What the design should change**
- [sources.md](../design/sources.md) "Fetch": make the refunded amount the **sum of `processed` refunds** from the refund list, not `amount_refunded`. Show pending refunds separately, or keep the row's status until they are processed. Keep `amount_refunded` and `refund_status` as a cross-check: log a warning when they disagree with the list, and don't fail on it.
- Fetch with `expand[]=refunds&expand[]=transaction` in **one call**, and fall back to the documented `/refunds` route (with `count=100`, paged) if the expand fails or returns exactly as many items as a page holds.
- Map status from the list: `captured` + processed refunds summing to 0 / between / equal to `amount` → succeeded / partially_refunded / refunded.

**Spike (laptop, owner's write).** S-Q6a from [razorpay-first.md](razorpay-first.md) Q6 already makes a normal refund. Fetch the payment once while that refund is still `pending` and record `amount_refunded` and `refund_status`. That settles the first half of this question.

## Q3: an authorised payment that is never captured

**Question.** Once the capture window ends, what are the payment's `status`, `captured`, `amount_refunded` and `refund_status`?

**Verdict: auto-refunded is documented. The resulting fields are not.**

**What the sources say.**
- "You must ensure that all payments in the authorized state are moved to the captured state within 3 days of creation… payments that are not captured within this period will be refunded automatically to customers." The timeouts run from 12 minutes to 3 days. The auto-refund speed is Normal, "refunded to your customer in 5-7 working days" (https://razorpay.com/docs/payments/payments/capture-settings/). Already in [razorpay-first.md](razorpay-first.md) Q7(b).
- The FAQ says uncaptured payments "are automatically refunded to the customers in 5 days" (https://razorpay.com/docs/payments/payments/faqs/). The late-authorisation page says "within 5 working days" (https://razorpay.com/docs/payments/payments/late-authorisation/handle/).
- The states table defines `refunded` only for captured payments ("You can refund the payments that have been successfully captured", https://razorpay.com/docs/payments/payments/). The one documented sample with `status: refunded` has `captured: true`, `amount_refunded` = `amount` and `refund_status: full` (https://razorpay.com/docs/api/qr-codes/fetch-payments/). No sample in the docs dump shows an uncaptured payment with a refund.
- The refund API refuses a refund unless the payment is `captured` ("The payment status should be captured for action to be taken", https://razorpay.com/docs/api/refunds/create-normal/). So an auto-refund of an uncaptured payment is not an ordinary refund, and whether it creates a refund entity or fires `refund.*` is not documented. The capture API mentions a payment "either already captured or has been voided" (https://razorpay.com/docs/api/payments/capture/), but `voided` is not a documented payment status.
- **Not documented:** the status (`refunded`? still `authorized`?), `captured` (presumably false), `amount_refunded` and `refund_status`. [razorpay-first.md](razorpay-first.md) Q7 left this open as spike S-Q7a.

**What the design should change**
- [sources.md](../design/sources.md) "Fetch": map **`refunded` with `captured: false` → `canceled`** (our lifecycle's "authorized → canceled: released or expired"). Map `refunded` with `captured: true` → refunded. Until S-Q7a reports, also treat `authorized` whose `created_at` is more than 3 days old as stale. Leave it as `authorized` and flag it in `doctor`; don't invent a status.
- The ledger's "authorized amount" for Razorpay is `amount` while `status` is `authorized` (see Q5).

**Spike.** S-Q7a as written in [razorpay-first.md](razorpay-first.md) Q7, plus: record `captured`, `amount_refunded`, `refund_status`, `GET /v1/payments/{id}/refunds` and any `refund.*` or `payment.*` webhooks once the timeout passes. Set the manual-capture timeout to 12 minutes in the test Dashboard so the spike takes minutes, not days.

## Q4: the card brand and other method details

**Question.** Which method fields are on the entity without `expand`? What do `expand[]=card`, `emi` and `upi` add? Which fields are personal data?

**Verdict: documented, with one conflict between pages about `card`.**

**What the sources say.**

*Always present (live, all seven test payments; fetch page field list).* `method`, `card_id`, `bank`, `wallet`, `vpa`, `international`, `email`, `contact`, `notes`, `description`, `order_id`, `invoice_id`, `acquirer_data`, `error_*`, `fee`, `tax`, `amount`, `amount_refunded`, `refund_status`, `captured`, `currency`, `status` and `created_at`. Unused ones are `null` (https://razorpay.com/docs/api/payments/fetch-with-id/).
- `method`: the entity page lists `card`, `netbanking`, `wallet`, `emi`, `upi` (https://razorpay.com/docs/api/payments/entity/). The fetch page adds **`paylater`**, whose sample carries `wallet: "rzpx_postpaid"` (https://razorpay.com/docs/api/payments/fetch-with-id/). Treat the list as open.
- `bank`: a 4-character bank code ("`UTIB` for Axis Bank"), set for netbanking and EMI. The live netbanking payments carried codes such as `ALLA` and `CNRB`. `wallet`: the wallet name, for example `payzapp` (entity page).
- Sometimes present, per the fetch page's samples: `customer_id` and `token_id` on a saved-card payment, `upi` on a UPI payment, and `amount_captured` (see Q5).

*`card`: the pages disagree.* The entity page's sample has a full `card` object inline on a captured card payment (https://razorpay.com/docs/api/payments/entity/). The fetch page's card sample has only `card_id`, with no `card` object (https://razorpay.com/docs/api/payments/fetch-with-id/). A separate page, "Fetch a Payment with Expanded Card Details", exists for `?expand[]=card` (https://razorpay.com/docs/api/payments/fetch-payment-expanded-card/). There is also `GET /v1/payments/{id}/card`, which returns the card alone (https://razorpay.com/docs/api/payments/fetch-card-details-payment/). Live: no card payment exists on the account. On a netbanking payment, `expand[]=card` added `"card": null`. So **ask for `expand[]=card`** rather than rely on the inline object.

*What the expansions add* (live on a netbanking payment, and the pages above).
- `card` (with `expand[]=card`): `id`, `entity`, `name` (the cardholder's name), `last4` (typed `integer` on the page, a string such as `"0153"` in every sample), `network` (the brand: `American Express`, `Diners Club`, `Maestro`, `MasterCard`, `RuPay`, `Unknown`, `Visa`), `type` (`credit`, `debit`, `prepaid`, `unknown`), `issuer` (a 4-character bank code, not set for foreign cards), `international`, `emi`, `sub_type`, `token_iin`.
- `emi` (with `expand[]=emi`): `{issuer, rate, duration}` on an EMI payment (https://razorpay.com/docs/api/payments/fetch-payment-expanded-emi/). Live, it added both `emi: null` and an undocumented `emi_plan: null`.
- `upi`: **there is no `upi` expand.** The API rejected it: "Value of each expand must be one of following types: card, emi, transaction, transaction.settlement, refunds, offers, token, settlement". The `upi` object comes inline on UPI payments, as `{payer_account_type, vpa, flow}` (fetch page UPI sample). `flow` is `intent`, `collect` or `in_app`. `payer_account_type` is `bank_account`, `credit_card`, `wallet` or `credit_line`.
- Also accepted: `offers` (a collection), `token` (null here), `transaction` and `transaction.settlement` (Q6), and `refunds` (Q2). `expand[]=settlement` returned 400 "Action not allowed." Several expands can go in one call: `refunds`, `transaction`, `card`, `emi`, `offers` and `token` together returned 200.

*Personal data.* Personal: `email`, `contact`, `vpa` and `upi.vpa`, `card.name`, `card.last4`, and `notes`, which hold whatever the merchant put there. The expanded-card sample's own `notes` carry an email and a phone number (https://razorpay.com/docs/api/payments/fetch-payment-expanded-card/). Linkable identifiers: `customer_id`, `token_id`, `card_id` and `acquirer_data` (`rrn`, `auth_code`, `arn`, `bank_transaction_id`, `upi_transaction_id`). The docs don't classify any of these; the classification is ours. `card.network`, `card.type`, `card.issuer`, `bank` and `wallet` are not personal by themselves.

**What the design should change**
- [sources.md](../design/sources.md) "Fetch": method is `method`, plus `card.network` (from `expand[]=card`) for cards, `bank` for netbanking and EMI, `wallet` for wallets and paylater, and nothing extra for UPI. Never put `vpa`, `last4` or `card.name` in the sheet. The design doesn't need them, and leaving them out keeps the sheet free of payment-instrument data.
- Masking ([data-model.md](../design/data-model.md) `customer_email`): `notes` can carry personal data. The metadata column needs the same masking option as email.

## Q5: partial capture

**Question.** Can `POST /v1/payments/{id}/capture` capture less than the authorised amount? Does `amount` change after capture?

**Verdict: no partial capture, and `amount` doesn't change.**

**What the sources say.**
- The request's `amount` "should be equal to the order amount". The error list includes 400 "Capture amount must be equal to the amount authorized", with "The amount you are trying to capture differs from the authorised amount" (https://razorpay.com/docs/api/payments/capture/).
- The same page also lists "Payment amount is greater than the amount due for order… when partial payments have already been captured on the order". That is about an **order** paid by several payments, not about a payment captured in part.
- The entity has one `amount` and no authorised-amount field (https://razorpay.com/docs/api/payments/entity/). The fetch page lists `amount_captured`, "The amount captured for this payment" (https://razorpay.com/docs/api/payments/fetch-with-id/). But its own UPI sample shows `amount_captured: null` on a captured payment, so do the payment-link webhook samples (https://razorpay.com/docs/webhooks/payment-links/), and none of the live responses had the key at all.
- Not tried live: capture needs an `authorized` payment, and none exists under auto-capture.

**What the design should change.** The authorised amount = `amount`, which stays fixed. Ignore `amount_captured`. Partial capture is a Stripe-only case.

## Q6: settlement, and payments in other currencies

**Question.** Is there a settlement amount, currency, id or date per payment? For non-INR payments, what currency are `fee`/`tax` in, and are there `base_amount`/`base_currency`?

**Verdict: nothing on the payment itself. There is an undocumented expand and a documented recon list. Non-INR payments carry `base_amount`/`base_currency`.**

**What the sources say.**

*On the payment.* No settlement field on the entity page or in any live response (https://razorpay.com/docs/api/payments/entity/). The Dashboard shows "settlement details" per payment (https://razorpay.com/docs/payments/payments/dashboard/), but no API page documents a per-payment settlement read.

*`expand[]=transaction` (undocumented, live).* It is listed only in the expand validation message, quoted on https://razorpay.com/docs/api/payments/fetch-all-payments-with-expanded-card-details/. The transaction field meanings are documented only on the settlement recon page (https://razorpay.com/docs/api/settlements/fetch-recon/). Live, on four captured INR payments it returned:

```json
"transaction": {"id": "txn_…", "entity": "transaction", "entity_id": "pay_…", "type": "payment",
  "amount": 50000, "currency": "INR", "fee": 1180, "tax": 180, "debit": 0, "credit": 48820,
  "on_hold": false, "settled": false, "settled_at": 1791271800, "settlement_id": null,
  "posted_at": null, "credit_type": "default", "created_at": 1790969354}
```

- `credit` = `amount − fee` on all four (48820, 48820, 48702, 98). That matches G41's "net is `amount − fee`".
- `settled_at` was **filled while `settled: false`**, with future times: 13:00 IST on Monday 5 October for payments captured on Friday evening, and 13:00 IST on Tuesday 6 October for two captured just after midnight IST. So before settlement it looks like the **scheduled** settlement time. The recon page defines it only as "Unix timestamp when the transaction was settled". Not documented.
- `settlement_id` is null until settled. `transaction.settlement` (`expand[]=transaction.settlement`) added `settlement: null`.
- On `failed` and `created` payments, `transaction` is `null`.
- The transaction does not change with refunds. The ₹500 payment with ₹200 refunded still showed `credit` 48820. Refunds settle as their own `type: refund` transactions (recon page).

*Settlement recon (documented).* `GET /v1/settlements/recon/combined?year&month[&day]` lists settled transactions with `entity_id`, `type`, `amount`, `currency`, `fee`, `tax`, `credit`, `debit`, `settled`, `settled_at`, `settlement_id`, `settlement_utr`, `order_id`, `method` and `card_network` (https://razorpay.com/docs/api/settlements/fetch-recon/). It is keyed by **settlement date**, so it can't be asked about one payment ([razorpay-first.md](razorpay-first.md) Q7(d)).

*Other currencies.* "Razorpay Settlements occur in INR based on the conversion rate at the time of payment." On a non-INR payment, `base_currency` is "The conversion currency that will be used to calculate fees and settlements. This currently defaults to INR and is present only if the `currency` is not INR". `base_amount` is "The converted payment amount that will be used to calculate fees and settlements", in the smallest unit of `base_currency` (https://razorpay.com/docs/payments/international-payments/currency-conversion/). That page's sample: `amount` 100 USD, `base_amount` 7129 INR, `fee` 207, `tax` 0. A fee of 207 is 2.9% of 7129 paise but 207% of 100 cents, so **`fee` and `tax` are in `base_currency` (paise)**. That is inferred from the sample and the "used to calculate fees" wording. No page states it outright. The webhook samples carry `base_amount` even on INR payments (`"base_amount": 100` on an INR `payment.captured`, https://razorpay.com/docs/webhooks/payments/), but no live INR fetch returned it. International payments need the feature enabled ([FAQ](https://razorpay.com/docs/payments/international-payments/faqs/)), so this can't be tried on the test account.

*Test-mode fees.* The two most recent ₹500 netbanking payments had `fee` 1180 and `tax` 180, which is 2% + GST. That is the advertised rate, where [razorpay-first.md](razorpay-first.md) Q5 saw 1298/198 (2.2%) on an earlier one. Test fees vary by payment, which makes them all the more illustrative.

**What the design should change**
- [sources.md](../design/sources.md) "Fetch":
  - **net**: in `base_currency` when present: `base_amount − fee`. Otherwise `amount − fee` in `currency`. Better: take `transaction.credit` when the transaction is present. Never subtract an INR fee from a USD amount.
  - **fee currency**: `base_currency` if present, else `currency`. The row needs a fee currency, or it must refuse non-INR payments in version 1.
  - **settlement amount and currency**: `transaction.credit` in `transaction.currency` from `expand[]=transaction`. Null while there's no transaction (pending). Mark the expand **undocumented**, and fall back to nulls if it is ever rejected.
  - Settlement id and date only once `settled: true`. Don't show `settled_at` before then, because it is then only a schedule.
- [data-model.md](../design/data-model.md): `settlement_minor` for Razorpay is the payment's gross credit. Refunds are debited from later settlements, not from this one.

## Q7: the customer's name, and `description`

**Question.** Is there a name field on a payment, or only `email` and `contact`? Is there a `description`?

**Verdict: no name. `description` exists.**

**What the sources say.**
- The entity's customer fields are `email` ("Customer email address used for the payment") and `contact` (https://razorpay.com/docs/api/payments/entity/). No `name` field. The only `name` is `card.name`, "Name of the cardholder" (Q4), which is often `""` in samples. The live responses agree.
- A name exists elsewhere: on a Customer (`GET /v1/customers/{id}`, when the payment has a `customer_id`, https://razorpay.com/docs/api/customers/), and on a Payment Link's `customer.name` (https://razorpay.com/docs/api/payments/payment-links/entity/). The test link here was made without a customer, so its `customer` was `{}`.
- `description` is "Description of the payment, if any". Live it was `null` for plain orders, `"#<link id>"` for the link payment, and the Checkout `description` option's text for the scripted Checkout payments. `email` can be `null` (UPI sample on https://razorpay.com/docs/api/payments/fetch-with-id/).

**What the design should change.** `customer_name` for Razorpay: null, unless a later version reads `customer_id` → Customer. Say so in the column notes. `description` comes from `description`.

## Q8: rate limits, timeouts and error shapes

**Question.** What guidance is there on rate limits and timeouts for the payments API? What do 5xx and 429 bodies look like?

**Verdict: as G54 and [razorpay-first.md](razorpay-first.md) Q7(f) settled.** No limit is published, and a 429 says `BAD_REQUEST_ERROR` "Too many requests", so classify by status. This note adds the 5xx bodies and the facts about razorpay-python.

**What the sources say.**
- The status meanings: 429 "Throttling Error… Retry the request after some time"; also 500, 502, 503 and 504; "watch for HTTP status code 429… Use an exponential backoff/stepped backoff strategy… Add some randomisation" (https://razorpay.com/docs/api/understand/).
- 5xx bodies. The common-errors page gives `{"error": {"code": "SERVER_ERROR", "description": "We are facing some trouble completing your request at the moment. Please try again shortly.", "source": "NA", …}}` (https://razorpay.com/docs/errors/common/). Only the **RazorpayX** errors page maps codes to statuses: 500 → `SERVER_ERROR` (reason `server_error`), 502 → `GATEWAY_ERROR` (`gateway_error`), 503 → `SERVER_ERROR` (`service_unavailable`), all with `source: internal` (https://razorpay.com/docs/errors/x/). That page covers payouts. Payments pages don't give the mapping.
- Timeouts: **not documented** for the API. The only 5-second timeout in the docs is the one Razorpay applies to our webhook endpoint.
- Live: no rate-limit headers on any response, as razorpay-first saw. Responses carry an `Uber-Trace-Id` header, worth logging on errors. A route miss returns `{"message": …}` with no `error` object. An encoded slash in the path returns an HTML 404. Each call took about 0.9-1.4 s from this cloud session, through its proxy.
- razorpay-python 2.0.1 (`razorpay/client.py`):
  - It passes **no timeout** to `requests`, so a call can hang forever. A `timeout=` keyword reaches `requests` (`client.payment.fetch(id, timeout=10)`), because the client passes `**kwargs` through.
  - Retries happen only after `client.enable_retry(True)`, and only on `ConnectionError` and `Timeout`: 5 attempts, starting at 1 s, doubling, capped at 60 s, with ±25% jitter.
  - On any non-2xx it reads `error.code` and raises `BadRequestError`, `GatewayError` or `ServerError` with the description only. **The HTTP status is lost**, so a 429 arrives as `BadRequestError("Too many requests")`. A body without `error` raises `ServerError("")`, and a non-JSON body raises a JSON decode error.

**What the design should change**
- [sources.md](../design/sources.md) "Fetch": the adapter should call the API with `httpx`/`requests` directly, using `client.auth` only, or wrap the SDK's session to keep the status. Use an explicit timeout (for example connect 5 s, read 15 s). Classification:
  - transient: 429; 5xx; a timeout or connection reset ([razorpay-first.md](razorpay-first.md) API runs); a 404 or other non-JSON body from the edge, once.
  - `not_found`: 400 with the Q1 description.
  - dead: any other 4xx.

## Q9: timestamps

**Question.** Is `created_at` Unix seconds UTC? Is there a `captured_at`?

**Verdict: Unix seconds. No `captured_at`.**

**What the sources say.**
- `created_at` is "Timestamp, in UNIX format, on which the payment was created" (https://razorpay.com/docs/api/payments/entity/). [razorpay-first.md](razorpay-first.md) Q3 showed the unit is seconds: the list filters reject millisecond values. Unix time counts from 1970-01-01 UTC, so the value has no time zone. The Dashboard's local display is a separate matter.
- `captured_at` appears on no page in the docs dump and in no live response. Neither do `authorized_at` or `updated_at` ([razorpay-first.md](razorpay-first.md) Q7(a)).
- Nearest stand-ins:
  - `transaction.created_at` from `expand[]=transaction` (undocumented). It was 3-30 s after the payment's `created_at` on the four captured payments, consistent with the time of capture.
  - The top-level `created_at` of a `payment.captured` webhook. That is the event's time, not a field of the payment ([razorpay-receiver-facts.md](razorpay-receiver-facts.md) Q4).

**What the design should change.** The row's date is the payment's `created_at`, as [data-model.md](../design/data-model.md) already says. If a "captured on" column is ever wanted, use `transaction.created_at`, labelled as approximate.

## API runs

Run 2026-10-02, about 19:20-19:57 UTC, from the cloud session against `https://api.razorpay.com`, with the owner's test key (prefix `rzp_test_` checked by the helper before each run; key sent as HTTP Basic from the environment; never printed). **GET only.** Nothing was created, captured or refunded. Personal fields (`email`, `contact`, `vpa`, `name`, `last4`, bank references) were redacted before printing. Ids below are test-mode ids, shortened.

| # | Call | Status | Shape seen |
|---|---|---|---|
| 1 | `/v1/payments?count=100` | 200 | 7 payments: 4 `captured` (one with `amount_refunded` 20000, `partial`), 1 `failed`, 2 `created`; all netbanking, INR |
| 2 | `/v1/refunds?count=100` | 200 | 2 refunds, both `processed`, on one payment (`speed_requested` normal and optimum; both `speed_processed: normal`) |
| 3 | `/v1/payments/{captured, failed, created}` | 200 | Keys as in Q4; no `card`, `upi`, `base_amount`, `captured_at`, `amount_captured`, `customer_id`; **`notes` is `[]` when empty and an object when set** |
| 4 | `/v1/payments/{pay_00000000000000, abc, pay_, one char changed, no prefix, 15 chars, order_…, rfnd_…, %20}` | 400 | `BAD_REQUEST_ERROR` "The id provided does not exist", `source: internal`, `step: payment_initiation`, `reason: input_validation_failed` |
| 5 | `/v1/payments/pay_abc%2Fdef` | 404 | HTML page, not JSON |
| 6 | `/v1/payments/PAY_…` (upper-case prefix) | 200 | The payment, id in lower case |
| 7 | `/v1/payments/{unknown well-formed}/refunds`; a real payment with none | 200 | `{"count": 0, "entity": "collection", "items": []}` for both |
| 8 | `/v1/payments/{abc, pay_, pay_abc, 15 chars}/refunds` | 404 | `{"message": "no Route matched with those values"}` |
| 9 | `/v1/payments/{no prefix, order_…}/refunds` | 400 | "<id> is not a valid id", `source: business` |
| 10 | `/v1/payments/{id}/refunds` (`count=1`, `count=101`); `/refunds/{rfnd}`; `/refunds/rfnd_0…0` | 200, 400, 200, 400 | Page of 1; "The count must be no greater than 100."; the refund; "invalid request sent" |
| 11 | `/v1/payments/{id}?expand[]=` card, emi, transaction, transaction.settlement, refunds, offers, token | 200 each | Adds `card: null`; `emi: null` + `emi_plan: null`; `transaction` (Q6); the same plus `settlement: null`; `refunds` collection; `offers` collection; `token: null` |
| 12 | `…?expand[]=settlement`; `…?expand[]=upi`, `bogus` | 400 | "Action not allowed."; "Value of each expand must be one of following types: card, emi, transaction, transaction.settlement, refunds, offers, token, settlement" (`field: expand.0`) |
| 13 | `…?expand[]=refunds&…transaction&…card&…emi&…offers&…token` | 200 | All six added in one response |
| 14 | `…?foo=1` | 200 | Unknown query parameter ignored on the single fetch |
| 15 | `?expand[]=transaction.settlement` on all 4 captured, 1 failed, 1 created | 200 | Captured: `credit = amount − fee`, `settled: false`, future `settled_at`, `settlement_id: null`; failed and created: `transaction: null` |
| 16 | `/v1/payments/{netbanking}/card`; `/v1/payments/pay_0…0/card` | 400, 400 | "Payment was not done using card"; "The id provided does not exist" |
| 17 | `/v1/payment_links`, `/v1/orders?count=5` | 200 | The link's `customer` is `{}`; orders `paid`, `attempted`, `created` |
| 18 | Headers on 200 / 400 / route-404 | (as above) | `Uber-Trace-Id` on API responses; no rate-limit headers; route 404 has a short header set |

## Sources

- **Razorpay docs** (each read as `<url>.md`; searched through https://razorpay-881012b3.mintlify.site/llms-full.txt):
  - API: https://razorpay.com/docs/api/payments/entity/, /api/payments/fetch-with-id/, /api/payments/fetch-payment-expanded-card/, /api/payments/fetch-payment-expanded-emi/, /api/payments/fetch-card-details-payment/, /api/payments/fetch-all-payments-with-expanded-card-details/, /api/payments/capture/, /api/refunds/entity/, /api/refunds/create-normal/, /api/refunds/fetch-multiple-refund-payment/, /api/settlements/fetch-recon/, /api/customers/, /api/payments/payment-links/entity/, /api/qr-codes/fetch-payments/, /api/understand/
  - Payments: /payments/payments/, /payments/payments/capture-settings/, /payments/payments/faqs/, /payments/payments/dashboard/, /payments/payments/late-authorisation/handle/, /payments/international-payments/currency-conversion/, /payments/international-payments/faqs/
  - Refunds: /payments/refunds/, /payments/refunds/faqs/, /payments/refunds/normal/, /payments/refunds/batch/, /payments/refunds/errors/
  - Errors: /errors/, /errors/common/, /errors/x/
  - Webhooks: /webhooks/payments/, /webhooks/refunds/, /webhooks/payment-links/
- **Razorpay source:** razorpay-python 8960507 (2.0.1): `razorpay/client.py`, `razorpay/errors.py`, `razorpay/constants/error_code.py`, `razorpay/resources/base.py`, `razorpay/resources/payment.py`, `tests/mocks/`.
- **Earlier notes cited:** [razorpay-first.md](razorpay-first.md) (Q1, Q3, Q5, Q6, Q7, Q8), [gaps-google-python-razorpay.md](gaps-google-python-razorpay.md) (G39, G41, G54 via sources.md), [razorpay-receiver-facts.md](razorpay-receiver-facts.md) (Q4).
- **Runs, 2026-10-02:** api.razorpay.com with the test key, as listed above.
- **Not tried:** any write (capture, refund, order or payment). A card, UPI, EMI or non-INR payment (none exist, and making one needs a browser or a feature request). A live-mode id. A 429 or 5xx (not provoked on purpose).
