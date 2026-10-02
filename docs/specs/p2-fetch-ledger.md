Status: implemented

# P2: fetch each payment from Razorpay and keep its ledger row

Feature issue: #19

## Plain-language summary

P1 stores every genuine Razorpay delivery with a waiting "fetch this payment" job. P2 does that job: the ledger worker asks Razorpay for the payment's current state in one call, turns it into one business row (status, amount, refunds, fee, GST, net, method, metadata, customer email, description), and writes it to the `payments` table, the ledger, so that however many events arrive and in whatever order, the row ends up equal to what Razorpay says. When a row really changes, a job is queued for the Google Sheet, which P3 will drain. Payments Razorpay doesn't know, throttling, outages and a revoked key each end the way the design says, and every rule is proved by tests that replay real test-mode answers.

**Rests on:** [build-order.md](../design/build-order.md) step P2; [ledger-worker.md](../design/ledger-worker.md), [sources.md](../design/sources.md) (Razorpay: fetch), [data-model.md](../design/data-model.md) (`payments`), [queue.md](../design/queue.md) (error classes), [edge-cases.md](../design/edge-cases.md) C1-C13, D1-D5, E1-E2, F1-F7; decision records [0001](../design/decisions/0001-database-is-the-ledger-sheet-is-a-mirror.md), [0002](../design/decisions/0002-fetch-current-state-not-event-payload.md), [0004](../design/decisions/0004-queue-in-the-database-behind-an-interface.md); grilling log Q69, Q70, Q73, Q76, Q97, Q109, Q110 and round 13, [Q122-Q131](../design/grilling-log.md); facts in [razorpay-fetch-facts.md](../research/razorpay-fetch-facts.md) (G64-G66) and [the P0 recordings](../../tests/recordings/razorpay/README.md); the code P1 built ([p1-receiver-queue.md](p1-receiver-queue.md), "Decisions made during the build"). The design pages stay the source of truth; this spec holds only P2's behaviour and its Assumptions.

## Problem Statement

After P1, a delivery becomes an event and a waiting ledger job, and nothing more: the owner has no ledger. Razorpay's events arrive out of order, repeat, and carry a snapshot that may already be stale; its fee and GST appear only at capture; refunds move from pending to processed later; an authorisation can be auto-refunded uncaptured; and its API answers an unknown id with a 400, gives no rate limit, and can be down. Without a worker that fetches the current state and writes it exactly once per change, the Payments tab, verify and every later promise have nothing true to show.

## Solution

`wsync serve` now runs the ledger worker on P1's stage runner. For each claimed ledger job it waits for a rate-limit token, fetches `GET /v1/payments/{id}?expand[]=refunds&expand[]=card` with no transaction open, maps the answer to a business row, and in one short transaction upserts the row (only if its row hash changed), enqueues one mirror job per configured sink, and completes the job. A payment still `created` makes no row. A payment Razorpay doesn't know keeps its last row flagged "not found at source", or ends dead when there is no row. Throttling, server errors and timeouts back off and retry; a 401 or 403 pauses the source. A new migration adds the `payments` table.

## User Stories

1. As the owner, I want every handled Razorpay event to end as one row per payment in the ledger, so that I have a single true record of each payment.
2. As the owner, I want the row to equal Razorpay's current state however many events arrived and in whatever order, so that a late or repeated event can never write an older state.
3. As the owner, I want events for one payment that arrive close together to cost one fetch, so that the API budget isn't wasted.
4. As the owner, I want an event that arrives while its payment is being fetched to cause exactly one more fetch, so that the newest change is never missed.
5. As the owner, I want a payment still `created` (nothing attempted) to make no row, so that abandoned checkouts don't clutter the ledger.
6. As the owner, I want `authorized`, `captured` and `failed` payments shown as authorized, succeeded and failed, so that the status reads the same across sources.
7. As the owner, I want a captured payment with processed refunds shown as partially refunded or refunded by the processed amount, so that money still pending return isn't counted as returned.
8. As the owner, I want a refund that fails to leave the refunded amount where the processed refunds put it, so that the row stays true after a failed refund.
9. As the owner, I want a payment with more refunds than one page holds to still sum all of them, so that a busy payment isn't under-counted.
10. As the owner, I want an authorisation that was never captured and was auto-refunded shown as canceled, so that it isn't mistaken for a refund of money I received.
11. As the owner, I want a late authorisation (failed, then authorized days later) to update the same row, so that I see the payment really went through.
12. As a bookkeeper, I want the fee and GST on the fee from Razorpay's own fetch, shown as pending while Razorpay hasn't set them, so that I never see a guessed or zero fee.
13. As a bookkeeper, I want net = amount − fee in the settlement currency, null while the fee is pending, so that the net figure is exact.
14. As a bookkeeper with an international payment, I want the fee, GST and net in the settlement currency (base amount − fee) and the amount in the payment's own currency, so that no figure mixes two currencies.
15. As the owner, I want amounts kept as integers in minor units with their currency, so that no rounding creeps in.
16. As the owner, I want a negative or non-integer amount from Razorpay to send the job to dead, so that a broken answer is never written.
17. As the owner, I want an unknown currency still written, so that an unusual payment isn't dropped.
18. As a bookkeeper, I want the method shown with one non-personal detail (card network, bank, wallet), so that the sheet matches Indian sales reports without card numbers or UPI ids.
19. As a buyer, I want my configured `notes` keys (such as an order id) carried into the row, so that the sheet joins to my own orders.
20. As the owner, I want empty notes (which Razorpay sends as `[]`) handled as no metadata, so that a quirk of the API doesn't fail a job.
21. As the owner, I want the customer's email and the payment's description kept as Razorpay reports them, and the phone number never stored, so that the ledger holds only what it needs.
22. As the owner, I want each row to record the event that last changed it and whether that change came by delivery or sweep, so that I can trace any row back.
23. As the owner, I want a fetch that changes nothing to write nothing and queue nothing for the sheet, so that a replay or duplicate costs one API call and no sheet write.
24. As the owner, I want a real change to bump the row's version and queue one job per configured sink, so that P3's flusher brings the sheet up to date.
25. As the owner, I want the upsert, the mirror jobs and the job's completion to commit together, so that a crash leaves either all of them or none.
26. As the owner, I want a worker whose lease was taken over to have its write rolled back, so that a stalled fetch can't overwrite a newer one.
27. As the owner, I want Razorpay never called inside a database transaction, so that a slow API can't block deliveries.
28. As the owner, I want a payment Razorpay answers "The id provided does not exist" for to keep its last values and be flagged "not found at source" when it has a row, so that deleted test data or a key mix-up is visible without losing history.
29. As the owner, I want such a payment with no row to end as a dead job naming a possible mode or account mix-up, so that a misconfiguration can't pass silently.
30. As the owner, I want a 429, a 5xx, a timeout or a dropped connection to retry with backoff, honouring Razorpay's `Retry-After` as a minimum, so that a busy or failing API recovers on its own.
31. As the owner, I want a 401 or 403 to pause the Razorpay source and keep the job's attempt, so that a revoked key doesn't burn through every job's attempts.
32. As the owner, I want any other unexpected answer (another 400, a non-JSON body) to send the job to dead with the reason, so that a broken case is visible instead of retried forever.
33. As the owner, I want fetches limited to 10 a second, so that a burst never triggers Razorpay's throttling.
34. As the owner, I want each fetch to give up after a fixed timeout, so that a hung connection can't hold a job forever.
35. As the owner, I want logs to name the job, payment and outcome of each fetch and never print keys, emails or phone numbers, so that logs stay shareable.
36. As the developer of P3, I want mirror jobs with stage `mirror` and sink `sheets` waiting for me, so that the flusher has work from the first P2 payment.
37. As the developer of P4, I want a fetch I can call for any payment id and a mapping I can feed a listed state into, so that the sweeper and the re-list reuse them.
38. As the owner on Windows, I want the worker to behave the same there, so that the demo runs on my laptop.

## Implementation Decisions

**Modules** (from the package layout in [build-order.md](../design/build-order.md); P1's code is the base):

- **Schema, migration `0002`**: the `payments` table with the columns of [data-model.md](../design/data-model.md), primary key `(source, account, payment_id)`; amounts as integers in minor units; `metadata` and `unknown_columns` as JSON text; `dispute_status` present and always null in P2; `row_hash`, `row_version`, `last_event_id`, `via`, `not_found_at`, `updated_at`. A matching table definition joins P1's schema module.
- **sources, the fetch**: the adapter interface gains `fetch(payment_key)` returning one of: the fetched state, `not_found`, or a classified failure (class `retry` with an optional `Retry-After`, `pause` of the source, or `dead` with a reason). The Razorpay fetch calls the API directly with async httpx (basic auth with `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET`, a fixed timeout), never the synchronous SDK, which drops the status and has no timeout. One call, `GET /v1/payments/{id}?expand[]=refunds&expand[]=card`; when the expanded refunds list holds 10 or more items (a full default page), `GET /v1/payments/{id}/refunds?count=100&skip=…` pages through every refund until a page returns fewer than 100 (a collection's `count` is the items on that page, not a total, so it can't show the list is incomplete; recorded in #20). Classification is by HTTP status: 400 whose description is "The id provided does not exist" → `not_found`; other 400 → `dead`; 401 and 403 → `pause` source; 429, 5xx, timeouts, connection errors → `retry`, with `Retry-After` read as seconds or an HTTP date; a 2xx that isn't a JSON object → `dead`. A token bucket of 10 a second for the source sits before every call.
- **sources, the mapping**: a pure function from the fetched Razorpay state (plus the configured metadata keys) to a business row, or "no row" for `created`. Status per [sources.md](../design/sources.md) "Fetch" and Q128: `authorized` → authorized; `failed` → failed; `captured`, or `refunded` with `captured: true` → succeeded, partially_refunded or refunded by the sum of processed refunds; `refunded` with `captured: false` → canceled. `refunded_minor` = sum of `processed` refunds. `amount_minor` = `amount`, `currency` as given; `authorized_minor` null (Razorpay can't capture part). `fee_minor`, `fee_tax_minor` from `fee`, `tax` (null = pending); `settlement_currency` = `base_currency` or `currency`; `net_minor` = (`base_amount` or `amount`) − `fee`, null while the fee is null; `settlement_minor` null (Q130). `method` per Q129. `metadata` = the configured keys found in `notes` (`[]` = none). `customer_email` and `description` as reported; `customer_name` null; never `contact`, VPA, card last4 or card name. `source_created_at` from the payment's `created_at`. An amount that is negative or not an integer → `dead`.
- **ledger, the worker**: the ledger stage's work function for P1's stage runner. Order: rate-limit token → fetch with no transaction open (heartbeats come from the runner) → map → return the writes. The writes, in the runner's transaction: read the current row; if there is no row and the state is "no row", nothing; otherwise compute `row_hash` over the fixed business-column list (Q124) and, only if it differs from the stored one (or there is no row), upsert with `row_version + 1`, `last_event_id` = the job's `caused_by_event_id`, `via` from that event's origin (`sweep` or `backfill` → `sweep`, otherwise `delivery`; no causing event → unchanged), `updated_at`, and enqueue one mirror job (stage `mirror`, sink = the sink's name) per configured sink. Not found with a row: keep its values, set `not_found_at` if not already set (a real change, so the hash includes it), complete. Not found without a row: the job fails `dead` with "not found at source: possible mode or account mix-up". A later successful fetch clears `not_found_at`. A prefetched state on the job is not produced before P4; the worker still uses it instead of fetching when present (F18).
- **Wiring in `serve`**: the app's lifespan builds one `claimed_by` (host, process id, start time) and one shared pause set, and registers the ledger runner (stage `ledger`, sink `ledger`, claim limit 1) with the configured retry profile. The configured sinks for mirror jobs are those present in the config (`[sinks.sheets]` → `sheets`).
- **Logs**: one line per fetch outcome with `job_id`, `payment_key`, the outcome and, for failures, the class and HTTP status; never keys, emails, phone numbers or bodies. New fields join the log allow-list.
- **Fixtures**: real test-mode answers recorded from the owner's account with the expand parameters, redacted (email to a placeholder, contact removed), saved beside P0's recordings; Razorpay's documented samples, marked `unrecorded`, cover what test mode can't produce (an international payment, a failed refund, an auto-refunded authorisation).

## Testing Decisions

**What a good test is here.** It feeds the ledger worker from outside and asserts only on what an outsider sees: the `payments` row and its columns, the jobs and their states (ledger done, dead or waiting; mirror jobs created or not), the `attempts` rows and log records. It never asserts on private helpers or call order. Razorpay is faked at the HTTP boundary with respx, answering with recorded test-mode bodies; time is moved with time-machine; SQLite is real.

**The seam** (one, the highest): **the ledger stage running on P1's stage runner**, its jobs created either by a signed delivery through the HTTP app (the end-to-end tracer) or by `enqueue` directly, with Razorpay's API answered by respx. Every mapping rule, every error class, not found, change detection, mirror jobs, rerun and fencing are proved through it. The mapping is pure, so its table of status cases may also be run directly as a table test; that is the only exception.

**Modules tested:** the Razorpay fetch, the mapping, the ledger worker and the `0002` migration, through the seam above. A subprocess test confirms `wsync serve` starts with the ledger stage registered (its fetches pointed at an unreachable base URL, so jobs retry rather than call Razorpay).

**Prior art:** P1's tests: the HTTP app fixtures, `enqueue`/`claim` helpers, the stage-runner tests with an injected sleep, `time_machine.move_to(..., tick=False)`, and the subprocess helpers.

## Out of Scope

- The Sheets sink, the flusher, Sync status, Daily summary, masking the email in the sheet, `wsync resume` (P3).
- The sweeper, the re-list, backfill and the state hash, verify (P4); P2 produces no prefetched states.
- FakeRazorpay replaying recordings as a full fake, the five scenarios, crash points, load checks (P5).
- `wsync demo`, `doctor`, the weekly real run, the README (P6).
- Disputes (the column stays null), `wsync replay`, the "pause after 5 retry failures in a row" rule, probes and automatic resume, alerts.
- Settlement amounts (`settlement_minor` stays null), Stripe, Postgres.

## Further Notes

- G66 (what an expired uncaptured authorisation looks like) is still a spike; the canceled rule is keyed on `captured: false`, which is documented, and the test uses Razorpay's documented sample until a real one is recorded.
- P0's recordings show fee and tax null while authorised and set at capture (1180 and 180 on ₹500), and test refunds going pending → processed within seconds.
- `amount_refunded` and `refund_status` are not used: whether they count pending refunds is undocumented (G65).

## Decisions made during the build

Settled by the implementers or in review, within what this spec and the design leave open:

- Python: worktrees and the main checkout run 3.14.8; pydantic fails to import on the bundled 3.14.0rc2.
- Refund paging: the fetch pages `/refunds` (100 a page, `skip`) when the expanded list holds 10 or more items, and stops at a short page (replaces A6's "count exceeds items", which can never fire).
- New setting `[sources.razorpay] api_url` (default `https://api.razorpay.com`), so tests and fakes point the fetch elsewhere; the fetch has a 10 s total timeout over the whole call.
- The configured sinks are the `[sinks.*]` sections present (`Config.sinks`). Mirror jobs carry the ledger job's `caused_by_event_id`.
- Method text reads `<method> (<detail>)`, e.g. `card (Visa)`, `netbanking (CNRB)`, `wallet (airtelmoney)`; `upi`, `emi`, `paylater` alone. Empty metadata is stored as `{}`.
- A `refunded` payment with `captured: true` whose processed refunds don't reach the amount is partially_refunded: the status follows the processed sum.
- An answer the mapping can't use (`UnusableState`: a bad amount, an unknown status) is an explicit `dead` failure. The 10-a-second bucket reuses the receiver's `RateCap`, one per adapter, before the main fetch and every refund page.
- Log fields `outcome` (`fetched`, `not_found`, `failed`), `error_class` and `http_status` join the allow-list. The instance id (`claimed_by`) is written `host:pid@start`.

## Assumptions

**Answered (owner accepted every recommendation)**

- A1 [CLIENT] P2's scope is the `0002` migration, the Razorpay fetch, the mapping and row hash, the ledger worker in `serve`, not found, the error classes and a 10/s token bucket; the sheet, sweeper, fakes, crash points, replay, disputes and the 5-failures pause are later. Each ticket ships its own tests (Q122).
- A2 [CLIENT] Mirror jobs are enqueued for each configured sink from P2 on and wait for P3 (Q123).
- A3 [HARD-TO-CHANGE] `row_hash` covers a fixed list of every business column, not bookkeeping columns or `via` (Q124).
- A4 [HARD-TO-CHANGE] The ledger stores `customer_email` as reported; masking is the sheet's; `contact` is never stored (Q125).
- A5 [HARD-TO-CHANGE] Razorpay's 400 "The id provided does not exist" is `not_found`: with a row, keep it and set `not_found_at`; without, the job ends dead naming a possible mode or account mix-up (Q126).
- A6 [HARD-TO-CHANGE] One fetch with `expand[]=refunds&expand[]=card`; `refunded_minor` and the refund statuses come from processed refunds; the refunds endpoint pages when the expanded list holds 10 or more items, since a collection's `count` is per page and can't show the list is incomplete (Q127; corrected in the build after #20's recordings).
- A7 [CLIENT] Razorpay `refunded` with `captured: false` → canceled (Q128).
- A8 [CLIENT] Method plus card network, bank code or wallet name; never last4, card name or VPA (Q129).
- A9 [HARD-TO-CHANGE] Fee and GST in the settlement currency (`base_currency` or `currency`); net = (`base_amount` or `amount`) − fee, null while pending; `settlement_minor` null for Razorpay (Q130).
- A10 [CLIENT] Metadata is only the configured `notes` keys; `[]` means none; no masking, the README warns (Q131).

**Decided internally**

- A11 [INTERNAL] The fetch uses async httpx with basic auth and a 10 s timeout, not razorpay-python — the SDK drops the HTTP status and has no timeout (G54, G64).
- A12 [INTERNAL] Classification is by HTTP status; `Retry-After` is read as seconds or an HTTP date — Razorpay's 429 body says `BAD_REQUEST_ERROR`.
- A13 [INTERNAL] `via` and `last_event_id` come from the job's `caused_by_event_id` and that event's origin; with none, `via` is unchanged — the column says how the latest change reached us.
- A14 [INTERNAL] A negative or non-integer amount sends the job to dead; an unknown currency is still written — edge cases D3, D4.
- A15 [INTERNAL] The lifespan builds one `claimed_by` (host, pid, start time) and one shared pause set; the ledger claim limit is 1 — queue.md.
- A16 [INTERNAL] The sources interface gains `fetch(payment_key)` returning a state, `not_found` or a classified failure; `not_found` is not a queue error class — the ledger worker turns it into a completion or a dead job.
- A17 [INTERNAL] A successful fetch after `not_found_at` was set clears it — the payment is back.
- A18 [INTERNAL] `authorized_minor` stays null for Razorpay — Razorpay rejects a capture for less than the authorised amount (G65 research, Q5).
- A19 [INTERNAL] The configured sinks for mirror jobs are the `[sinks.*]` sections present, today `sheets`; mirror jobs use stage `mirror` and the sink's name — data-model.md's jobs table.
- A20 [INTERNAL] Fixtures are real test-mode answers recorded with the expand parameters and redacted, plus documented samples marked `unrecorded` for cases test mode can't produce — Q113.

**Taken as given**

- A21 The owner's test account holds captured, failed, created and partly refunded payments to record from (seen in the research run).
- A22 G66 stays open; the canceled rule rests on `captured: false`.
- A23 One seam, the ledger stage on the stage runner fed by deliveries or `enqueue` with Razorpay faked by respx, is the owner's default (questions defaulted to the recommendations).
