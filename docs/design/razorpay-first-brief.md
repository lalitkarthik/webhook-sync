# Brief: Razorpay becomes the first source

> **Superseded on 2026-10-02 by grilling round 11 ([Q103-Q114](grilling-log.md)).** Kept as the record of what was asked. Where it disagrees with the design pages, the design pages win. Research ([razorpay-first.md](../research/razorpay-first.md)) corrected three of its statements: test payments can be made and paid by a script, not only by hand (G45); paying them needs no laptop, only receiving webhooks does; and there were five open Stripe spikes, not eight. Stripe is parked, not "built second" (Q103).

**What this page contains.** A decision the owner made on 1-2 October 2026 that the design pages do not yet reflect, what is known so far, the questions it opens, and the smallest product that is worth showing a buyer. It is the input to grilling round 11.

**How to read it.** Historical. Round 11 turned it into numbered decisions in [grilling-log.md](grilling-log.md) and edits to the design pages.

## The decision

**Razorpay is the first source. Stripe comes second.** This reverses Q2.

Why: Stripe is invite-only in India ("Businesses from India are not able to sign up", support.stripe.com). The owner is an individual in India and cannot open a Stripe account, so there is no Stripe test mode, no Stripe CLI and no recording spike. The owner has Razorpay **test-mode keys**.

What does not change: the core (receiver, ledger, jobs, fetch mode, flusher, sweeper, verify), every decision record, and the Stripe design pages, which stay as the design of the second source adapter.

## What is known about Razorpay

Confirmed against primary sources in [gaps-google-python-razorpay.md](../research/gaps-google-python-razorpay.md) (G38-G41):
- No events-list API. Payments and refunds are listed by creation time (`from`, `to`, `count` ≤ 100, `skip`); `GET /v1/disputes` exists.
- The signature is HMAC-SHA256 over the raw body, with no timestamp. The event id is in the `x-razorpay-event-id` header, outside the signature.
- A delivery times out after 5 s; retries run for 24 h, then the webhook is disabled.
- API keys cannot be limited to reading. Test keys start `rzp_test_`.
- The payment's `fee` already includes `tax` (GST), so net is `amount − fee`. `refund_status` is null, `partial` or `full`.

Read in Razorpay's docs on 1 October but **not yet written up or tried** (round 11's research confirms each):
- Test mode works on an account that has not finished KYC.
- A webhook needs a public URL. Razorpay's docs refuse `ngrok.io`, `webhook.site` and `requestbin.com` addresses and suggest zrok.
- There is no equivalent of `stripe trigger`. A test payment is a Payment Link made by API and paid by hand with a test card. Test mode allows 30 Payment Links.
- Resending one delivery by hand from the dashboard is not documented.

## Questions for round 11

Research first ([research-gaps.md](research-gaps.md) gets new rows), then grill:

1. **Making test payments from a script.** Payment Links paid by hand, a local Standard Checkout page, or a server-to-server payment API if test mode allows it. Decides how far `wsync demo` can be automatic.
2. **The duplicate-storm scene** without a resend button: replay a recorded, correctly signed delivery ourselves, or provoke Razorpay's own retries by answering 500. Decides what the GIF honestly shows.
3. **The outage scene** by polling: payments made while the server is down appear after restart through the sweeper's poll of `GET /v1/payments`. Confirm the list's ordering and whether `from`/`to` are inclusive.
4. **The tunnel** for real deliveries to the owner's laptop: which one Razorpay accepts.
5. **Fee and tax in test mode**: are they filled in on a test payment, and when?
6. **Refunds and disputes in test mode**: can a failed refund or a dispute be produced at all?
7. **The sweeper for a source with no events list**: synthetic events per payment state (sources.md already sketches this); what the checkpoint means; the hourly re-fetch of unsettled payments.
8. **Mode**: Razorpay events carry no test/live flag, so the key prefix is the only check.
9. **Stripe**: stays designed, built second, developed against fakes until a buyer's own account or an invite gives real access. The eight open Stripe spikes in research-gaps.md stay open.
10. **Build order**: Razorpay's 8-12 h moves in front; which of P0-P6 change.

## The smallest product worth showing: proposed, not yet agreed

The owner asked for only what hooks a buyer: proof of work first, everything else added on top later. Proposed scope, for round 11 to confirm:

**What a buyer sees (three things):**
1. **One row per payment, however often Razorpay delivers.** The same delivery sent five times ends as one row; the duplicates are counted.
2. **Payments made while the service was down come back.** After a restart the missing rows appear, marked `sweep`.
3. **A line of proof.** `wsync verify` prints "N payments, 0 differences" for Razorpay against the ledger against the sheet. The Payments tab shows amount, status, refund state, fee, GST on the fee, net and method.

**What has to exist underneath for those three to be true:**
- The receiver: raw-body signature check, dedupe on the event id and on the signed body's hash, fail-closed start, answers within milliseconds.
- The ledger on SQLite: one business row per payment, the event and its job written in one transaction.
- The queue: one live job per payment with rerun, retry with backoff, dead after N attempts.
- The ledger worker in fetch mode: `GET /v1/payments/{id}`, mapped to our statuses.
- The flusher: one batched write by key to a Payments tab and a Sync status tab, with the post-write check.
- The sweeper by polling in time slices, with its checkpoint, at startup, on a timer and on demand.
- `wsync serve`, `sweep`, `verify`, `demo`, and four `doctor` checks (secrets set, key prefix, sheet reachable, database writable).
- Tests that prove the three promises on fakes, plus three crash points.
- A README with the GIF, the three promises with the test that proves each, and honest limits.

**Added on top later, in the order buyers ask:** the Daily summary tab; disputes; pausing with probes (the first version stops and says why); the alert URL; admin pages; rebuild; backups and the hosting template; the heartbeat; the Stripe adapter; the forwarding sink; the database sink; currencies other than INR; the OAuth fallback; pruning; Hypothesis suites; Postgres.

## What a cloud session can and cannot do

Design, research, planning, building and testing on fakes all run in a cloud session ([docs/agents/cloud.md](../agents/cloud.md)). A cloud machine cannot receive a webhook, so three things stay on the owner's laptop: recording real Razorpay deliveries (with a tunnel), paying test Payment Links in a browser, and recording the GIF.
