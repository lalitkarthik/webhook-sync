# Research: Stripe gaps (G1, G2, G4-G8, G10, G14, G15, G27, G28, G30, G31, G35)

Researched 2026-10-01 against primary sources.

Primary means docs.stripe.com, support.stripe.com, the stripe-cli source on GitHub and the stripe-python source. The rows come from [research-gaps.md](../design/research-gaps.md). Where only a secondary source exists, it is named and not counted.

| Gap | Verdict | One-line finding | Source |
|---|---|---|---|
| G1 | confirmed | Every resource can be set to Read. Refunds share the "Charges and Refunds" permission, and balance transactions come under "Balance". Events and Webhook endpoints can both be granted. | docs.stripe.com/keys/permissions-reference |
| G2 | contradicted | With `automatic_async` capture, the default since 2024-04-10, `balance_transaction` is null on `payment_intent.succeeded`. It arrives with `charge.updated`, which Stripe promises within 1 hour. | docs.stripe.com/payments/payment-intents/asynchronous-capture |
| G4 | confirmed | Without `--webhook-endpoint`, the CLI adds `for_stripecli=true` and the event is replayed to the CLI. | stripe-cli `events_resend.go`; docs.stripe.com/cli/events/resend |
| G5 | confirmed | `stripe listen` acknowledges each event to Stripe before forwarding it. It makes one POST and never retries. Stripe's docs say nothing about retries to CLI sessions. | stripe-cli `proxy/webhook_event_processor.go`, `proxy/endpoint.go` |
| G6 | unclear | The docs don't say whether `amount_refunded` goes down when a refund fails. A test card makes a refund fail later. | docs.stripe.com/refunds, /testing |
| G7 | unclear | The docs back the fact: event `data` never changes, and `id`, `type`, `created` and `livemode` are fixed. But deliveries can be rendered at another API version. | docs.stripe.com/api/events/object, /webhooks |
| G8 | confirmed | 4000000000000259 and seven other cards create disputes. The evidence text `winning_evidence` or `losing_evidence` closes one as won or lost. | docs.stripe.com/testing |
| G10 | unclear | The docs say only that the payload is JSON. The CLI forwards Stripe's headers from a map, one value per name, but `-H` can add more. Test events carry an extra `v0` signature. | docs.stripe.com/webhooks; stripe-cli `endpoint.go` |
| G14 | confirmed | Every field the design reads exists. The balance transaction's `amount`, `fee`, `net` and `currency` are in the settlement currency. The balance transaction can be null (see G2). | docs.stripe.com/api/charges/object, /payment_intents/object, /balance_transactions/object |
| G15 | contradicted | The page doesn't list differences from ISO 4217. It gives 16 zero-decimal currencies and 4 special cases, and no three-decimal list any more. UGX appears in both lists. | docs.stripe.com/currencies |
| G27 | unclear | Nothing documents how soon an event can be listed. One secondary source saw events appear a few seconds late. | docs.stripe.com/api/events/list |
| G28 | unclear | No Stripe doc or SDK mentions `Date`. A live 401 from api.stripe.com carried one, and HTTP requires it. | observed 2026-10-01; RFC 9110 §6.6.1 |
| G30 | contradicted | Summing `charge` and `refund` is wrong three ways: other methods use `payment` and `payment_refund`, a partial capture books a `refund`, and failures and disputes have their own types. | docs.stripe.com/reports/balance-transaction-types, /reports/reporting-categories |
| G31 | confirmed | `data.latest_charge.balance_transaction` is 3 levels deep, and the limit is 4. The list takes `created[gte]`/`[lt]` and a limit from 1 to 100. | docs.stripe.com/api/expanding_objects, /api/payment_intents/list |
| G35 | confirmed | The list returns `url`, `enabled_events`, `api_version` (which can be null) and `status`, and needs `webhook_read`. `enabled_events` can be `['*']`. | docs.stripe.com/api/webhook_endpoints/object, /keys/permissions-reference |

## G1: restricted key permissions

**Assumed.** An `rk_test_` key, sandboxes included, can get read-only access to PaymentIntents, Charges, Refunds, Disputes, Events, Balance transactions and Webhook endpoints, and can't move money.

**Verdict: confirmed**, but the design's names don't match Stripe's.

**What the sources say.** A restricted key starts `rk_live_` or `rk_test_`. You set each resource to None, Read or Write, and None is the default. The guide says to create the first key in a sandbox. GET needs read permission; POST and DELETE need write, and Write includes Read. A read-only key therefore can't create charges, refunds or payouts.

The permissions reference maps each endpoint to a permission:

| Design says | Endpoint | Permission code | Name in the permission table |
|---|---|---|---|
| PaymentIntents | `GET /v1/payment_intents` | `payment_intent_read` | Payment Intents |
| Charges | `GET /v1/charges`, `/v1/charges/{id}` | `charge_read` | Charges and Refunds |
| Refunds | `GET /v1/refunds`, `/v1/refunds/{id}` | `charge_read` (the same one) | Charges and Refunds |
| Disputes | `GET /v1/disputes` | `dispute_read` | Payment Disputes |
| Events | `GET /v1/events` | `event_read` | Events |
| Balance transactions | `GET /v1/balance_transactions` | `balance_read` | Balance |
| Webhook endpoints | `GET /v1/webhook_endpoints` | `webhook_read` | Webhook Endpoints (flagged as sensitive) |

The names come from the Stripe Apps permissions table, which uses the same codes; the Dashboard's labels may differ slightly. A separate permission, `balance_transaction_source_read`, only lets a key expand a balance transaction's `source`, and the design doesn't need it. The reference has no row for `GET /v1/payment_intents/{id}`.

**Open point.** No page says which permission is needed to expand a *related* object, such as `latest_charge.balance_transaction` on a PaymentIntent read. The design expects a 403 on an optional read. Check that during the recording spike: make a key without `balance_read` and see whether the expand fails with a 403 or the whole request fails.

**What the design should change**
- [sources.md](../design/sources.md) "The key": name the permissions by code (`payment_intent_read`, `charge_read`, `dispute_read`, `event_read`, `balance_read`, `webhook_read`). Refunds need no permission of their own.
- `doctor`'s missing-permission message should give the code and the table name, such as "Charges and Refunds: Read".

URLs: https://docs.stripe.com/keys/restricted-api-keys, https://docs.stripe.com/keys/permissions-reference, https://docs.stripe.com/stripe-apps/reference/permissions

## G2: when `balance_transaction` is set

**Assumed.** `latest_charge.balance_transaction` is set by the time a PaymentIntent is `succeeded`. Where it arrives later, an event the design already handles fires.

**Verdict: contradicted.**

**What the sources say.** The asynchronous capture guide says that with `capture_method=automatic_async`, `balance_transaction` is null on the charge in the API response and in the `charge.succeeded` and `payment_intent.succeeded` events. Expanding `latest_charge.balance_transaction` may return nothing yet. The guide says to subscribe to `charge.updated`, whose `previous_attributes` shows `balance_transaction` going from null to an id. The service level for that event is 1 hour after confirmation. The PaymentIntent reference marks `automatic_async` as the default. The 2024-04-10 changelog made it the default whenever the creator doesn't set a capture method.

The buyer's checkout creates the PaymentIntent, at its own API version, so our pinned version doesn't decide this. Most card payments made through a current integration will succeed before their fee is known.

Delayed-notification methods such as ACH and direct debit get a pending balance transaction of type `payment` when the payment is created. If the payment later fails, a `payment_failure_refund` balance transaction follows. For those methods the fee can exist while the payment is still `processing`.

**What the design should change**
- [sources.md](../design/sources.md) handled types: add `charge.updated`, keyed by the charge's `payment_intent`. The sweep's type filter grows to 15 types, still under the limit of 20.
- [ledger-worker.md](../design/ledger-worker.md) and the column mapping: a `succeeded` payment with a null balance transaction writes fee, net and settlement as *pending*, not *unknown*, and raises no alert.
- [verify.md](../design/verify.md): payments younger than about 1 hour with a pending fee are "in progress", not stale. The Daily summary marks fees still to come.

URLs: https://docs.stripe.com/payments/payment-intents/asynchronous-capture, https://docs.stripe.com/changelog/2024-04-10/automatic-sync-default-paymentintents, https://docs.stripe.com/api/payment_intents/object, https://docs.stripe.com/reports/balance-transaction-types

## G4: `stripe events resend` and `stripe listen`

**Assumed.** `stripe events resend <evt>` delivers again to a running `stripe listen` session.

**Verdict: confirmed.**

**What the sources say.** The CLI reference says the command resends an event to the CLI's local webhook endpoint. To reach a registered endpoint you pass `--webhook-endpoint=we_…`. It only works for events created in the last 30 days. In the source, `events_resend.go` sends `POST /v1/events/{event}/retry`, and adds `for_stripecli=true` when `--webhook-endpoint` isn't given. The webhooks guide adds that a Dashboard resend works for 15 days.

The resend is a POST made with the CLI's own login key, not the service's read-only key. That suits the GIF, which is recorded from a developer's terminal.

URLs: https://docs.stripe.com/cli/events/resend, https://github.com/stripe/stripe-cli/blob/master/pkg/cmd/resource/events_resend.go, https://docs.stripe.com/webhooks (Manual retries)

## G5: nothing retries a failed `stripe listen` forward

**Assumed.** Stripe doesn't retry a delivery that `stripe listen` failed to forward, so events missed while the local server is down come back only through the sweeper. Does `stripe listen` retry?

**Verdict: confirmed, from the CLI source.** No Stripe doc covers it.

**What the sources say.** In `webhook_event_processor.go`, once an event parses, the CLI sends Stripe an acknowledgement. Only then does it forward the event, in a goroutine, with a TODO noting that errors aren't handled. `EndpointClient.Post` in `endpoint.go` makes a single request. On a connection error it prints `FailedToPostError` and returns, with no retry and no response sent to Stripe. The only retry loop in `proxy.go` is for opening the session. So Stripe has its acknowledgement, and nothing in the open source asks it to send again. Events raised while `stripe listen` itself isn't running never reach the CLI. `stripe events resend` (G4) or `/v1/events` can recover them.

For registered endpoints the webhooks guide says live mode retries for up to three days. A sandbox retries only three times over a few hours.

**What the design should change**
- [build-order.md](../design/build-order.md) and [showcase.md](../design/showcase.md): if the prototype GIF shows an outage catch-up while running on `stripe listen`, it needs the minimal sweeper, as research-gaps.md warned. Otherwise, cut that scene.
- [testing.md](../design/testing.md): tests against a registered sandbox endpoint must expect only three retries over a few hours, not three days.

URLs: https://github.com/stripe/stripe-cli/blob/master/pkg/proxy/webhook_event_processor.go, https://github.com/stripe/stripe-cli/blob/master/pkg/proxy/endpoint.go, https://github.com/stripe/stripe-cli/blob/master/pkg/proxy/proxy.go, https://docs.stripe.com/webhooks (Automatic retries)

## G6 (SPIKE): a failed refund and `amount_refunded`

**Assumed.** A failed refund lowers `latest_charge.amount_refunded`.

**Verdict: unclear, needs the spike.**

**What the sources say.** The charge reference defines `amount_refunded` as the amount refunded, which can be less than `amount`. It says nothing about failures. When a refund fails, its `status` becomes `failed`, it gains `failure_reason` and `failure_balance_transaction`, and a `refund.failed` event fires. The bank returns the money, which can take up to 30 days. A cancelled refund is treated as a kind of failure. The balance side is booked as `refund_failure` for cards, `payment_refund` for other methods, and in the past as `adjustment`.

**Spike.** Pay with test card 4000000000005126 (or `pm_card_refundFail`). Its refund starts `succeeded` and later turns `failed`. Refund part of it, then read the charge before and after `refund.failed`, and record `amount_refunded`, `refunded` and the refund list. If the amount doesn't go down, [sources.md](../design/sources.md) must work out the refunded total from refunds whose status is `succeeded` or `pending`.

URLs: https://docs.stripe.com/refunds (Handle failed refunds), https://docs.stripe.com/testing (Simulate an asynchronous refund), https://docs.stripe.com/api/charges/object

## G7 (SPIKE): stable fingerprint fields

**Assumed.** Resends, retries and `/v1/events` copies of one event keep the same `type`, `data.object.id`, `created` and `livemode`, even when their bytes differ.

**Verdict: unclear.** The docs point towards yes.

**What the sources say.** On the Event object, the contents of `data` never change, and `api_version` stays the version used when the event was created. The list endpoint renders each event at that version, whatever `Stripe-Version` the request sends. The webhooks guide says events can't be changed after they are created. A resend is `POST /v1/events/{id}/retry`, the same event again. Bytes will differ, though. A destination set to another API version gets the event rendered at that version, and `pending_webhooks` changes over time. The guide also warns that `created` is in whole seconds, so two events can share it. The design's fingerprint pairs it with the event's object id, so that is fine.

**Spike.** Capture one event three ways: live delivery through `stripe listen --latest`, `stripe events resend`, and `GET /v1/events/{id}`. Compare the four fields.

URLs: https://docs.stripe.com/api/events/object, https://docs.stripe.com/api/events/list, https://docs.stripe.com/webhooks (Event ordering, API versioning)

## G8: disputes in test mode

**Assumed.** Stripe test cards create disputes that move through the dispute statuses in test mode.

**Verdict: confirmed.**

**What the sources say.** The testing page lists cards whose charge succeeds and is then disputed:

| Card | Dispute |
|---|---|
| 4000000000000259 | fraudulent |
| 4000000000002685 | product not received |
| 4000000000001976 | an enquiry |
| 4000000404000079 | several disputes on one charge |

Others cover an early fraud warning, Visa CE 3.0, Visa and Mastercard compliance, and Smart Disputes. Each card has a `pm_card_…` equivalent. To settle a dispute, submit evidence as `uncategorized_text` through the API, or in the Dashboard's Additional information field:

| Evidence | Result |
|---|---|
| `winning_evidence` | closes the dispute as won and credits the amount and fees |
| `losing_evidence` | closes it as lost; an enquiry closes without escalating |
| `escalate_inquiry_evidence` | turns an enquiry into a full dispute |

`prevented` can only be produced by the dispute-prevention cards, and only if the test account is enrolled in Dispute Resolution or Deflection. Otherwise those cards create an ordinary dispute.

URL: https://docs.stripe.com/testing (Simulate a dispute, Evidence, Dispute prevention)

## G10 (SPIKE): content type and security headers

**Assumed.** Deliveries from an endpoint and from `stripe listen` send a JSON content type and each security header once.

**Verdict: unclear, needs the spike.**

**What the sources say.** The webhooks guide says Stripe sends a JSON payload and every request carries a `Stripe-Signature` header. It doesn't give the exact `Content-Type`. That header holds `t=` and one or more signatures. Test events also carry a fake `v0` signature, and during a secret roll there is one `v1` per active secret. The CLI passes on the headers Stripe sent over its websocket, as `map[string]string`, so one value per name. It then adds any `-H`/`--headers` values with `Header.Add`, so an operator can create a duplicate.

**Spike.** In the recording spike, log the raw headers of deliveries from an endpoint and from `stripe listen`. Checks 4 and 5 should accept any `application/json` media type whatever its charset, and should ignore `v0`.

URLs: https://docs.stripe.com/webhooks (Verify signatures manually), https://docs.stripe.com/cli/listen, https://github.com/stripe/stripe-cli/blob/master/pkg/proxy/endpoint.go, https://github.com/stripe/stripe-cli/blob/master/pkg/websocket/webhook_messages.go

## G14: the fetch returns what the mapping reads

**Assumed.** `GET /v1/payment_intents/{id}?expand[]=latest_charge.balance_transaction` returns fee, net, amount and currency in the settlement currency. The charge carries `disputed`, `amount_refunded`, `payment_method_details.type` and `card.brand`.

**Verdict: confirmed**, with G2's caveat that the balance transaction may not exist yet.

**What the sources say.**

- **PaymentIntent:** `amount_received`, `receipt_email`, and `latest_charge`, which can be expanded and is null until confirmation is attempted.
- **Charge:** `amount_refunded`, `disputed`, `balance_transaction` (nullable), `billing_details.email` and `.name`, `payment_method_details.type`, and `payment_method_details.card.brand`. Brands are `amex`, `cartes_bancaires`, `diners`, `discover`, `eftpos_au`, `jcb`, `link`, `mastercard`, `unionpay`, `visa` and `unknown`.
- **Balance transaction:** `amount`, `fee`, `net` (amount minus fee) and `currency`. Its worked example turns a 10.00 EUR charge into a 12.34 USD balance transaction with an `exchange_rate`, which confirms the settlement currency.

The charge also has its own `receipt_email`, but the design reads the PaymentIntent's first, which is fine.

URLs: https://docs.stripe.com/api/payment_intents/object, https://docs.stripe.com/api/charges/object, https://docs.stripe.com/api/balance_transactions/object

## G15: currency minor units

**Assumed.** Stripe's currency page lists every currency whose API minor units differ from ISO 4217.

**Verdict: contradicted.**

**What the source says.** Currencies are two-decimal unless the page says otherwise, and it lists two groups.

**Zero-decimal, 16 currencies:** BIF, CLP, DJF, GNF, JPY, KMF, KRW, MGA, PYG, RWF, UGX, VND, VUV, XAF, XOF, XPF.

**Special cases, 4 currencies:**

| Currency | What Stripe says |
|---|---|
| ISK | Now zero-decimal, but the API keeps it two-decimal with `00` cents: 5 ISK is `500`. |
| UGX | The same as ISK, although UGX is also in the zero-decimal list. |
| HUF | You can charge two-decimal amounts. Payouts must be divisible by 100. |
| TWD | You can charge two-decimal amounts. Payouts must be divisible by 100. |

The page has **no three-decimal list any more**; only its meta description still mentions three-decimal support. support.stripe.com names BHD, JOD, KWD, OMR and TND as supported three-decimal currencies, without an API rule. The page never compares itself with ISO 4217.

Compared with ISO 4217, which is not a Stripe source, the differences are these. ISK and UGX are 0 in ISO, but the API shows 2 decimals, if the special case wins over the list. MGA is 2 in ISO but 0 at Stripe. The three-decimal currencies have no documented rule at Stripe. The UGX entry contradicts itself, so a spike or Stripe support has to settle it.

**What the design should change**
- [data-model.md](../design/data-model.md) and [sheets-sink.md](../design/sheets-sink.md): keep our own exponent table built from Stripe's lists, not ISO. Default to 2; use 0 for the 16 zero-decimal currencies except UGX; use 2 for ISK and UGX. Record the source and date.
- If a payment arrives in a currency with no documented rule (BHD, JOD, KWD, OMR, TND, UGX), flag it on the row. Don't guess.
- Add G15 to the recording spike: a sandbox charge in UGX and in ISK, to record the `amount` Stripe returns.

URLs: https://docs.stripe.com/currencies, https://support.stripe.com/questions/which-payments-methods-and-products-are-available-in-the-uae

## G27: how soon an event can be listed

**Assumed.** An event becomes listable within 10 minutes of its `created` time.

**Verdict: unclear.**

**What the sources say.** The list reference and the undelivered-events guide cover the 30-day window, `delivery_success`, and chronological order with `ending_before`. They give no delay. The webhooks guide calls an event's data a snapshot that may be stale, but says nothing about when it can be listed. A secondary source, a Sequin blog post on dev.to, saw events turn up in the list a few seconds after newer ones. It used a 5-second guard, but didn't measure the worst case. That isn't confirmation.

**Spike.** For a day of sandbox traffic, poll `/v1/events` every 30 s and log the gap between `created` and first sight, and the largest one. Keep the 10-minute overlap unless that shows otherwise.

URLs: https://docs.stripe.com/api/events/list, https://docs.stripe.com/webhooks/process-undelivered-events; secondary: https://dev.to/acco/finding-and-fixing-eventual-consistency-with-stripe-events-a6i

## G28: an HTTP `Date` header on API responses

**Assumed.** Every Stripe API response carries an HTTP `Date` header that can serve as Stripe's clock.

**Verdict: unclear from the docs.** It was observed.

**What the sources say.** No Stripe doc mentions `Date`. stripe-python reads `Request-Id` and `Stripe-Should-Retry`, but never `Date`. On 2026-10-01 an unauthenticated `GET https://api.stripe.com/v1/events` returned a 401 carrying `Date` in the usual GMT format. RFC 9110 §6.6.1 says an origin server with a clock must send `Date`. The header only has one-second resolution.

**Spike.** Log `Date` on authenticated 200 responses in the spike. Keep the fallback (our clock minus a margin) for when the header is missing or can't be parsed.

URLs: https://www.rfc-editor.org/rfc/rfc9110#section-6.6.1, https://github.com/stripe/stripe-python/blob/master/stripe/_http_client.py

## G30: balance transactions as independent totals

**Assumed.** Balance transactions of type `charge` and `refund` over a window sum to the ledger's gross, fees and refunds. Newer methods may use `payment` and `payment_refund`, and a balance transaction's `created` isn't the payment's date.

**Verdict: contradicted.** The caveats hold, but summing those types gives false differences.

**What the sources say.** Card charges are `charge` and card refunds are `refund`. Other methods use `payment` and `payment_refund`. Stripe's `reporting_category` field groups them: `charge`+`payment` becomes `charge`, and `refund`+`payment_refund` becomes `refund`. Three things break a plain sum:

- **Partial capture.** When a capture is smaller than the authorisation, Stripe books a `charge` for the whole authorisation and a `refund` for the uncaptured part. Its reporting category is `partial_capture_reversal`. Summing `refund` would count that as a refund, which the ledger doesn't.
- **Failures.** A delayed method's `payment` can later be reversed by `payment_failure_refund`, category `charge_failure`. A card refund that fails comes back as `refund_failure`, and a returned non-card refund as a positive `payment_refund`.
- **Disputes.** Disputes and dispute reversals are `adjustment`, with categories `dispute` and `dispute_reversal` and a dispute as the source. Other Stripe fees are `stripe_fee`, `stripe_fx_fee` and `tax_fee`.

Timing differs too. `available_on` is when the net funds become available. A refund's balance transaction is created when the refund starts, which may be weeks after the payment. A charge's balance transaction appears after capture, up to an hour late with async capture (G2). Amounts are in the settlement currency, not the charge currency. `GET /v1/balance_transactions` filters by `created`, `type`, `currency`, `payout` and `source`, but not by `reporting_category`.

**What the design should change**
- [verify.md](../design/verify.md): list every balance transaction created in the window, and bucket it by `reporting_category`. Compare `charge` minus `charge_failure` with the ledger's settled gross. Compare `refund` minus `refund_failure` with refunds. `partial_capture_reversal` doesn't count as a refund. Compare the sum of `fee` over those with fees. Report `dispute` and `dispute_reversal` separately.
- Compare by the balance transaction's `created` against the ledger's event dates (capture time for gross, refund time for refunds), not the payment's creation date. Compare in the settlement currency.
- Allow for payments less than an hour old with no balance transaction yet (G2), and for refund failures still on their way (up to 30 days).

URLs: https://docs.stripe.com/reports/balance-transaction-types, https://docs.stripe.com/reports/reporting-categories, https://docs.stripe.com/api/balance_transactions/object, https://docs.stripe.com/api/balance_transactions/list

## G31: list PaymentIntents with a deep expand

**Assumed.** `GET /v1/payment_intents` accepts `expand[]=data.latest_charge.balance_transaction`, within the depth limit, at 100 a page.

**Verdict: confirmed.**

**What the sources say.** Expansion can go up to four levels deep. On list requests it starts at `data`; Stripe's own example is `data.payment_intent.customer.default_source` on a charge list. `data.latest_charge.balance_transaction` is three levels. The list takes `created` with `gt`, `gte`, `lt` and `lte`, plus `limit` from 1 to 100 (default 10). The expand guide warns that deep expansions on list requests slow them down. The same G2 caveat applies: listed payments less than an hour old may have no balance transaction yet.

URLs: https://docs.stripe.com/api/expanding_objects, https://docs.stripe.com/expand, https://docs.stripe.com/api/payment_intents/list

## G35: reading webhook endpoints

**Assumed.** `GET /v1/webhook_endpoints` returns each endpoint's `url`, `enabled_events` and `api_version`, and a restricted key can read it.

**Verdict: confirmed.** Two details matter.

**What the sources say.** The object has `url`, `enabled_events`, `api_version` and `status` (`enabled` or `disabled`). The `secret` is returned only when the endpoint is created, so reading endpoints doesn't expose it. `api_version` can be null, which means the account default. `enabled_events` can be `['*']`, meaning all events except those that must be picked by name. The list needs `webhook_read`, which Stripe marks as sensitive.

**What the design should change**
- [operations.md](../design/operations.md) and [sources.md](../design/sources.md) (`check_setup`): treat `['*']` as subscribed to every handled type. When `api_version` is null, say so: "account default, can't compare". Don't warn it differs. Also fail when `status` is `disabled`.

URLs: https://docs.stripe.com/api/webhook_endpoints/object, https://docs.stripe.com/keys/permissions-reference

## Sources

- https://docs.stripe.com/keys/restricted-api-keys
- https://docs.stripe.com/keys/permissions-reference
- https://docs.stripe.com/stripe-apps/reference/permissions
- https://docs.stripe.com/payments/payment-intents/asynchronous-capture
- https://docs.stripe.com/changelog/2024-04-10/automatic-sync-default-paymentintents
- https://docs.stripe.com/api/payment_intents/object
- https://docs.stripe.com/api/payment_intents/list
- https://docs.stripe.com/api/charges/object
- https://docs.stripe.com/api/balance_transactions/object
- https://docs.stripe.com/api/balance_transactions/list
- https://docs.stripe.com/reports/balance-transaction-types
- https://docs.stripe.com/reports/reporting-categories
- https://docs.stripe.com/api/expanding_objects
- https://docs.stripe.com/expand
- https://docs.stripe.com/api/webhook_endpoints/object
- https://docs.stripe.com/api/events/object
- https://docs.stripe.com/api/events/list
- https://docs.stripe.com/webhooks
- https://docs.stripe.com/webhooks/process-undelivered-events
- https://docs.stripe.com/cli/events/resend
- https://docs.stripe.com/cli/listen
- https://docs.stripe.com/testing
- https://docs.stripe.com/refunds
- https://docs.stripe.com/currencies
- https://support.stripe.com/questions/which-payments-methods-and-products-are-available-in-the-uae
- https://github.com/stripe/stripe-cli/blob/master/pkg/cmd/resource/events_resend.go
- https://github.com/stripe/stripe-cli/blob/master/pkg/proxy/webhook_event_processor.go
- https://github.com/stripe/stripe-cli/blob/master/pkg/proxy/endpoint.go
- https://github.com/stripe/stripe-cli/blob/master/pkg/proxy/proxy.go
- https://github.com/stripe/stripe-cli/blob/master/pkg/websocket/webhook_messages.go
- https://github.com/stripe/stripe-python/blob/master/stripe/_http_client.py
- https://www.rfc-editor.org/rfc/rfc9110#section-6.6.1
- Secondary, not counted: https://dev.to/acco/finding-and-fixing-eventual-consistency-with-stripe-events-a6i
