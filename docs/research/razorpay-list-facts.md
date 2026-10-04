# Research: Razorpay list facts for the sweeper (build step P4)

Researched 2026-10-03 against primary sources, for six questions raised while building P4. P4's sweeper lists `GET /v1/payments` and `GET /v1/refunds` by creation-time slices, and an hourly re-list re-reads payments created in the last 7 days and refunds created in the last 21 days. Each changed item becomes a synthetic event and a ledger job, whose listed state may be used instead of a fetch ("prefetched"). This note does not repeat what [razorpay-first.md](razorpay-first.md) Q3, Q7 and spike S-Q3a, [razorpay-fetch-facts.md](razorpay-fetch-facts.md), [gaps-google-python-razorpay.md](gaps-google-python-razorpay.md) (G32, G39) and research-gaps rows G50, G53 and G56 already settle. It cites them instead.

Primary means razorpay.com/docs, read as the Markdown copies Razorpay serves (`https://razorpay.com/docs/<path>.md`), searched through Razorpay's own full-text dump (`https://razorpay-881012b3.mintlify.site/llms-full.txt`), plus razorpay-python and razorpay-mcp-server source on GitHub. For healthchecks.io: its own docs pages and its source (`healthchecks/healthchecks` at 3addc67). The live calls went to api.razorpay.com with the owner's **test** key. The helper checked the `rzp_test_` prefix before running, and only GETs were made. That was 124 calls, at 07:52-07:56 UTC. No key or personal value was printed or stored. Nothing was pinged at healthchecks.io. No secondary source was used.

| Q | Question | Verdict | One-line finding |
|---|---|---|---|
| Q1 | `/v1/payments` paging and bounds | **settled by runs** (ties still open) | Max `count` 100, default 10. Newest first. `from` and `to` both inclusive (re-confirmed on 3 payments). Skip past the end gives 200 and an empty list. `from > to` gives 200 and an empty list. Milliseconds, negatives and decimals give 400. A missing bound means unbounded. `count=0`/`101` give 400. |
| Q2 | Expands on the list | **settled by runs: a listed payment can't replace the fetch when it has refunds** | `expand[]=card` is documented and accepted. `expand[]=refunds` is accepted but **returns an empty collection on every payment**, including one with 2 processed refunds. `expand[]=transaction` is **silently ignored**. |
| Q3 | `/v1/refunds` | **settled by runs** | Same paging, order and inclusivity. Each refund carries `payment_id`, `status`, `speed_processed` and `created_at`, with keys identical to `GET /v1/refunds/{id}`. An undocumented **`payment_id` filter works**. The undocumented **`status` filter is accepted but does not filter**. A millisecond `to` silently means "no upper bound". |
| Q4 | Headers, timing, rate limits | **settled by runs: nothing published** | No rate-limit headers. 20 parallel list calls all gave 200. Payments lists took 1.1-2.0 s, refunds lists 0.75-1.3 s. Refunds responses lack `Uber-Trace-Id` and use another error wording, so they come from another service. |
| Q5 | Consistency while paging; id cursor | **not documented; no cursor** | No page says anything about consistency under concurrent writes. `/v1/payments` rejects `after`, `before`, `starting_after`, `ending_before`, `cursor`, `sort` and `order`. `/v1/refunds` ignores them. The paging rule from razorpay-first Q3(d) stands. |
| Q6 | healthchecks.io pings | **documented** | `https://hc-ping.com/<uuid>[/start\|/fail\|/log\|/<0-255>]`, by HEAD, GET or POST. Success answers 200 with body `OK`, but **an unknown UUID and a rate-limited ping also answer 200** ("OK (not found)", "OK (rate limited)"). At most 5 pings a minute per check. The docs suggest a 10 s timeout and retries. |

## Q1: `GET /v1/payments` with `from`, `to`, `count` and `skip`

**Question.** What are the maximum `count` and the ordering? Are `from` and `to` inclusive at a payment's exact `created_at`? What do skip past the end, `from > to`, milliseconds, negative values, missing bounds and `count=0`/`101` return?

**Verdict: settled by runs.** These runs agree with razorpay-first Q3, which was run on 2 October, and add the edge cases. The order of payments created in the same second is still unknown. The account has no ties, so spike S-Q3a stays open.

**What the sources say.**
- *Count.* "Default value is 10. Maximum value is 100" (https://razorpay.com/docs/api/payments/fetch-all-payments/). Live: `count=101` gave 400 "The count may not be greater than 100." (`field: count`). `count=0` and `count=-1` gave "The count must be at least 1.", and `count=abc` gave "The count must be an integer.". Those are the page's own error texts (same URL, Errors). The default of 10 couldn't be seen live, because the account holds only 7 payments.
- *Order.* **Not documented**: the page says only "By default, only the last 10 records are displayed" (same URL). Live: strictly descending `created_at` on every call. `count=1` with `skip` 0 and 6 gave the newest and the oldest. `count=3&skip=2` gave the 3rd to 5th. Same as razorpay-first Q3(a). Ties: not documented, not observable here.
- *Inclusive bounds.* The endpoint page says `from` is "from when" and `to` is "till when" (same URL). The general page says "after which" and "before which" (https://razorpay.com/docs/api/understand/, Pagination). Neither states inclusivity. Live, with T = each payment's exact `created_at`, `from=T&to=T` returned that payment, for the newest (`…8h2378`, T=1790970020), an inner one (`…WcsMeE`, 1790969938) and the oldest (`…DwqGru`, 1790956537). `from=1790969939&to=1790970019` returned nothing. **Both ends are inclusive.** That confirms razorpay-first Q3.
- *Skip past the end.* `count=1&skip=7` on 7 payments, `count=100&skip=7` and `skip=1000000` each gave **200** `{"entity":"collection","count":0,"items":[]}`. No error, no `has_more`. Not documented.
- *`from > to`.* `from=1790970020&to=1790969938` gave 200 and an empty list. Not documented; same as razorpay-first Q3(c).
- *Milliseconds, negative, decimal.* `from` or `to` in milliseconds, or `-1`, gave 400 "from (to) must be between 946684800 and 4765046400" (documented on the same page, Errors). `from=1790969938.5` gave 400 "from must be an integer." (documented). The range error carries no `field`. The count and skip errors do.
- *Missing bounds.* Only `from=T` returned every payment with `created_at ≥ T`. Only `to=T` returned every payment with `created_at ≤ T`. Neither bound returned the whole account. An empty `from=` was treated as absent (200, all 7). Not documented. **A forgotten bound widens the call instead of failing it.**
- *`skip`.* `skip=-1` gave "The skip must be at least 0.", and `skip=abc` gave "The skip must be an integer." (documented on the same page).

**What the design should change**
- [sources.md](../design/sources.md) "Sweep and polling" already says newest first, both bounds inclusive. Keep it, and add **"a missing bound means unbounded"**. The adapter must always send both `from` and `to`, and assert them (it already asserts `from ≤ to` and the range). Paging stops at a short page, never at an error, because skip past the end is a 200.

## Q2: expands on the list, and whether a listed payment can stand in for a fetch

**Question.** Does `GET /v1/payments` accept `expand[]=refunds` and `expand[]=card`, as the single fetch does? If not, what does a listed payment lack?

**Verdict: settled by runs. A listed payment can't replace the fetch for a payment that has refunds, whatever expand is asked for.**

**What the sources say.**
- *Documented.* The list takes `expand[]` with `card` ("Expanded card details, usable for card and EMI payments") and `emi`. Its sample shows a full `card` object, `network` included, on each listed card payment (https://razorpay.com/docs/api/payments/fetch-all-payments-with-expanded-card-details/). The plain list page has no `expand[]` (https://razorpay.com/docs/api/payments/fetch-all-payments/). `refunds` and `transaction` are not documented for the list.
- *Live, `expand[]=card`.* 200. Each listed item gained `card: null`. All 7 are netbanking, so a card object couldn't be seen; the docs sample is the only evidence that it fills in for card payments.
- *Live, `expand[]=refunds`.* 200, and every item gained `refunds: {"entity":"collection","count":0,"items":[]}`, **including `pay_Tj9gmEfed6kRqO`, which has two processed ₹100 refunds** (`amount_refunded` 20000, `refund_status: partial` on the same item). The single fetch with the same expand returned `refunds.count` 2 with both items (`payment-fetch-expanded-…json`). Tried on 4 calls: plain, a one-second window around that payment, `count=1&skip=2`, and `status=captured`. All gave the same empty collection. Not documented. **The expand is accepted and returns wrong data.**
- *Live, `expand[]=transaction` and `transaction.settlement`.* 200, with **no `transaction` key** on any item. The expand is silently ignored. `expand[]=settlement` gave 200 and added nothing, where the single fetch gives 400 "Action not allowed." ([razorpay-fetch-facts.md](razorpay-fetch-facts.md) Q4). `emi` added `emi: null` and `emi_plan: null`. `offers` added an empty collection, and `token` added `token: null`, as on the single fetch. `expand[]=bogus` gave 400 with the same allowed list as the single fetch (`field: expand.0`).
- *Live, `expand=refunds` (no brackets).* **HTTP 500 `SERVER_ERROR`** "We are facing some trouble completing your request at the moment." So a client bug here looks like a transient server fault.
- *What a listed payment has.* Its keys equal the single fetch's, minus `card` and `refunds`, plus `amount_captured: null`. The single fetch omits `amount_captured`; [razorpay-fetch-facts.md](razorpay-fetch-facts.md) Q5 saw it on no fetch. Every state-hash field is present: `status`, `captured`, `amount`, `amount_refunded`, `refund_status`, `fee`, `tax`, `error_code`, `error_reason` and `notes` (`payments-list-count100.json`). So the hash can be computed from the list, but the row can't be.
- *What that breaks.* P2's mapper reads the refunded amount from `refunds.items` (the sum of processed refunds, never `amount_refunded`; `src/webhook_sync/sources/razorpay.py` `_processed_refunds`, [razorpay-fetch-facts.md](razorpay-fetch-facts.md) Q2). Given a listed payment, with or without `expand[]=refunds`, it sees no refunds. A partly refunded payment would be written as `succeeded` with nothing refunded. The row hash changes, so the wrong row is written and mirrored.

**What the design should change**
- [sweeper.md](../design/sweeper.md) "Razorpay: polling", and the manual-backfill row ("Lists payments … with their state, as ledger jobs"): for Razorpay, **a listed payment is prefetched only when `amount_refunded == 0` and `refund_status` is null**. Any other listed payment is enqueued **without** a prefetched state, so the worker makes P2's fetch. A refund's job (from the refund list) **always fetches**. Ask for `expand[]=card` on the list (documented), so that a card payment's prefetched state has `card.network`. **Never send `expand[]=refunds` or `expand[]=transaction` on a list**: the first lies and the second is ignored.
- [sources.md](../design/sources.md) "Listing for verify and backfill" (the Razorpay part): say the listed state is complete for Stripe but not for Razorpay. Then F18's "nothing is fetched twice" holds only for unrefunded Razorpay payments.
- The mapper (P2's `to_row`) should **refuse a state with no `refunds` key whose `amount_refunded` > 0**, as a dead-class bug, instead of mapping it to zero refunds. That guards against a wrong prefetched state.
- Keep the query builder tested: `expand=` without brackets is a 500, which would be retried as transient.
- The residual risk is a `pending` refund that isn't yet in `amount_refunded`. Whether pending refunds count there is undocumented (G65; [razorpay-fetch-facts.md](razorpay-fetch-facts.md) Q2). It is harmless: the row counts processed refunds only, and the refund list's own job fetches once that refund changes.

## Q3: `GET /v1/refunds`

**Question.** The Q1 questions, asked of refunds. Does each listed refund carry `payment_id`, `status`, `speed_processed` and `created_at`? Is there a `payment_id` filter?

**Verdict: settled by runs.** The order and inclusivity match payments. The validation differs. One undocumented filter works and one doesn't.

**What the sources say.**
- *Documented.* `from` ("Unix timestamp at which the refunds were created"), `to`, `count` ("maximum of 100") and `skip`. "By default, only the last 10 refunds are returned" (https://razorpay.com/docs/api/refunds/fetch-all/). No `payment_id`, `status` or `expand` parameter is documented. The page's own Failure sample is "The payment id field is required." with `field: payment_id`, and it explains that error as "A GET API is executed by POST method" (same URL, Errors). The per-payment route is documented separately (https://razorpay.com/docs/api/refunds/fetch-multiple-refund-payment/). razorpay-python's `Refund.all` passes any `data` through (https://github.com/razorpay/razorpay-python/blob/master/razorpay/resources/refund.py). razorpay-mcp-server's `FetchAllRefunds` exposes only `from`, `to`, `count` and `skip` (https://github.com/razorpay/razorpay-mcp-server/blob/main/pkg/razorpay/refunds.go).
- *Order and inclusivity, live.* Newest first: `…OulF3Q` (1790970053), then `…5WLvww` (1790970043). `from=T&to=T` returned each one, and `from=1790970044&to=1790970052` returned neither. **Both ends are inclusive.** Skip past the end and `from > to` gave 200 and an empty list. A missing bound means unbounded, as on payments.
- *Validation, live (differs from payments).*
  - `from=-1` and `to=-1` gave 400 "The from (to) must be no less than 0."
  - A decimal or `abc` gave "The from must be an integer."
  - `count=0` gave "The count must be at least 1.", `count=-1` gave "…no less than 1.", and `count=101` gave "…no greater than 100.". `skip=-1` gave "The skip must be no less than 0.". These texts are documented (same page, Errors).
  - **No upper range check.** A millisecond `from` gave 200 and an empty list (as G56 found). A **millisecond `to` gave 200 and every refund**, which is a silent "no upper bound".
  - No error carries `field`.
- *Fields, live.* Every listed refund has `id`, `entity`, `amount`, `currency`, `payment_id`, `notes`, `receipt`, `acquirer_data`, `created_at`, `batch_id`, `status`, `speed_processed` and `speed_requested`. That is the same key set as `GET /v1/refunds/{id}` (`refund-fetch-…json`). The entity page documents these fields (https://razorpay.com/docs/api/refunds/entity/). **A listed refund is a complete refund.**
- *`payment_id` filter (undocumented), live.* `payment_id=pay_Tj9gmEfed6kRqO` returned its 2 refunds. `payment_id` of a payment with no refunds, or of `pay_00000000000000`, returned 0. Combined with `from=to=1790970053`, it returned just that one refund. **It filters.** It answers an unknown payment with 200 and an empty list, as the per-payment route does ([razorpay-fetch-facts.md](razorpay-fetch-facts.md) Q1).
- *`status` filter (undocumented), live.* `status=pending`, `status=failed` and `status=processed` each returned **both processed refunds**. `status=created` gave 400 "The status must be a valid value.". So it is **validated but ignored**. razorpay-first Q3(f) called it a "validated refund `status` filter" without testing whether it filtered.
- *Other names, live.* `expand[]=payment`, `starting_after` and `after` gave 200 and were ignored. Unknown names are ignored, as G56 found.

**What the design should change**
- [sources.md](../design/sources.md) "Sweep and polling": add that refunds accept a millisecond `to` as "no upper bound". The existing guard (check seconds and range before calling) already covers it; name both bounds. Add **never use `status` on `/v1/refunds`**: it is accepted and ignored.
- The refund re-list can trust each listed refund as complete. The refund's own state hash (`status`, `amount`, `speed_processed`) needs nothing more.
- `payment_id` on `/v1/refunds` is undocumented. Prefer the documented `GET /v1/payments/{id}/refunds` that P2 already pages. Don't build on the filter.

## Q4: response headers, timing and rate limits

**Question.** Do list responses carry anything about rate limits? How long does a list of 100 take?

**Verdict: nothing is published, and nothing appears in the headers.** [razorpay-first.md](razorpay-first.md) Q7(f) and [razorpay-fetch-facts.md](razorpay-fetch-facts.md) Q8 (G54) settled the limits. This adds list timing.

**What the sources say.**
- *Docs.* "Razorpay uses a request Rate Limiter… watch for HTTP status code 429… Use an exponential backoff/stepped backoff strategy" (https://razorpay.com/docs/api/understand/, Rate Limiting; the same text is on https://razorpay.com/docs/api/x/). No number is published. The only page in the docs dump that names `Retry-After` is the Route linked-account onboarding API ("retry after the duration specified in the `Retry-After` header"; the composite linked-account page under https://razorpay.com/docs/api/payments/route/), not the payments or refunds lists.
- *Headers, live (`headers-timing.json`).*
  - Payments list: `date`, `content-type: application/json`, `transfer-encoding: chunked`, `connection: close`, `cache-control`, `expires`, `pragma`, `strict-transport-security`, `uber-trace-id`, `via: rws`, `x-frame-options`, `x-pam` and `x-xss-protection`.
  - Refunds list: `date`, `content-type: application/json; charset=utf-8`, `content-length`, `connection`, `strict-transport-security`, `via` and `x-xss-protection`. **No `uber-trace-id`.**
  - Neither has any rate-limit or `Retry-After` header.
- *Timing, live (cloud session, through its proxy; the 7-payment account, so `count=100` returned 7 items).*
  - Payments list: 1.46-1.83 s over 10 sequential calls; 1.05-2.05 s across all runs.
  - Refunds list: 0.75-1.34 s.
  - 20 payments lists on 10 threads: all 200, 1.44-1.79 s each, no 429.
  - Error answers: about 1.05-1.2 s on payments, 0.75-0.87 s on refunds.
  - The time for a page of 100 real items couldn't be measured.

**What the design should change.** Nothing new. Keep "429 by status, back off with jitter, honour `Retry-After` when present (none seen)". Allow at least 2 s per list call when sizing the sweep. 10 slices × 2 lists × 2 s is about 40 s for a 60-hour catch-up, well inside a 10-minute timer. Log `uber-trace-id` when present; refunds won't have it.

## Q5: consistency while paging, and an id cursor

**Question.** Is there any documented guarantee about the list's consistency while payments are created (drift under `skip`)? Is there an `id`-based cursor?

**Verdict: not documented, and there is no cursor.**

**What the sources say.**
- The list pages and the general Pagination section describe `count` and `skip` only ("This can be used for pagination, in combination with `skip`", https://razorpay.com/docs/api/understand/). They say nothing about consistency, ordering under concurrent writes, or snapshotting (https://razorpay.com/docs/api/payments/fetch-all-payments/, https://razorpay.com/docs/api/refunds/fetch-all/). The full docs dump has no passage on pagination consistency or cursors for these lists. Its only "cursor" hits are about the Cursor editor (llms-full.txt, searched 2026-10-03).
- Live: `/v1/payments` rejects `after`, `before`, `starting_after`, `ending_before`, `cursor`, `sort` and `order` with 400 "<name> is/are not required and should not be sent" (`payments-cursor-and-sort-params.json`). Earlier runs rejected `id` and `created_at` too (razorpay-first Q7(d)). `/v1/refunds` ignores `starting_after` and `after`: same first item, so no effect. **There is no id cursor and no sort parameter.** Disputes and settlements carry `has_more`, but payments and refunds don't (razorpay-first Q3).
- Drift couldn't be provoked live: that needs a payment created mid-page, and this run was GET only. The reasoning in razorpay-first Q3(d) stands. With a `to` in the past, no new item can enter the window. With an open `to`, newest-first order makes a new item **repeat** one at a page boundary, never miss one. Ties in one second are the only undocumented risk. The design's paging rule (shrink `to`, dedupe by id, `skip` only inside a stuck two-second window) doesn't depend on any of this.

**What the design should change.** Nothing. [sweeper.md](../design/sweeper.md)'s paging rule is right, and its "doesn't rely on the order of items created in the same second" is still needed. One detail: the hourly re-list must also give an explicit `to` (= `source_now − 60 s`), not an open one, so that new payments can't shift its pages.

## Q6: healthchecks.io pings

**Question.** From the official docs: the ping URL forms (success, `/start`, `/fail`, `/<exit-code>`), methods, timeouts, rate limits, and what a 200 body is.

**Verdict: documented. One trap: a 200 doesn't mean the ping counted.**

**What the sources say** (https://healthchecks.io/docs/http_api/ unless noted).
- *URLs.*
  - Success `https://hc-ping.com/<uuid>`.
  - Start `…/<uuid>/start`.
  - Failure `…/<uuid>/fail`.
  - Log `…/<uuid>/log`: "without signaling success or failure… The check's status will remain the same".
  - Exit status `…/<uuid>/<exit-status>`, "a 0-255 integer… 0 as a success and all other values as a failure".
  - The same five exist by slug: `https://hc-ping.com/<ping-key>/<slug>[/…]`. "If you make a Pinging API request using a non-unique slug, Healthchecks.io will return the '409 Conflict'".
  - An optional `?rid=<uuid>` pairs a start with its completion.
- *Methods and transport.* "HEAD, GET, and POST request methods". A POST body up to 100 kB is stored. HTTP and HTTPS, HTTP/1.0 to HTTP/2, IPv4 and IPv6. A check can be set to "Only POST" (https://healthchecks.io/docs/configuring_checks/). The source then turns a GET into an ignored ping and still answers `OK` (`hc/api/views.py` `ping()`: `if check.methods == "POST" and method != "POST": action = "ign"`, https://github.com/healthchecks/healthchecks/blob/master/hc/api/views.py).
- *Response.* "Successful responses will have the '200 OK' HTTP response status code and a short 'OK' string in the response body". The example has `Content-Type: text/plain`, `Content-Length: 2` and an optional `Ping-Body-Limit: 100000` header. **But the documented codes also include "200 OK (not found): Could not find a check with the specified UUID" and "200 OK (rate limited): Rate limit exceeded, request was ignored"**. A 400 means "invalid url format". The self-hosted source answers an unknown UUID with **404 "not found"** instead (same file). So the hosted service and a self-hosted one differ.
- *Rate limit.* "If you ping a check more than 5 times per minute, some of the requests may get rate limited and not recorded to the database. In extreme cases we will block the client's IP address."
- *Timeouts and retries* (https://healthchecks.io/docs/reliability_tips/). "An explicit per-request time limit mitigates this problem… `curl -m 10`", and "instruct your HTTP client to retry failed requests several times… `curl --retry 5`". The Python example uses `timeout=10` and catches `requests.RequestException` (https://healthchecks.io/docs/python/). The service publishes no server-side timeout: **not documented**.
- *`/start` semantics* (https://healthchecks.io/docs/measuring_script_run_time/). "If a job sends a 'start' signal but does not send a 'success' signal within its configured grace time, Healthchecks.io will assume the job has failed". Grace time then also means the maximum run time (https://healthchecks.io/docs/configuring_checks/). Period and grace limits on the free plan are in G32.

**What the design should change**
- [sweeper.md](../design/sweeper.md) "The dead-man's switch": ping the success URL **by POST** (works under "Only POST" too), with a 10 s timeout, never blocking or failing the sweep. Ping **once per sweep pass**, not per slice, so a catch-up can't pass 5 a minute. Don't use `/start`: a long catch-up would trip the grace time. Don't use `/fail`: Q96 alerts on silence, so that a short source outage doesn't page anyone.
- [operations.md](../design/operations.md) `doctor` ("heartbeat URL set and reachable"): treat the ping as good only if the body is exactly `OK`. Report "OK (not found)" as a wrong UUID, and any 404 as a wrong URL on a self-hosted instance. Have `doctor` ping `/log` (or `/<uuid>/log`), which proves the URL without changing the check's state.
- The `heartbeat_url` example `https://hc-ping.com/…` in [operations.md](../design/operations.md) is right. Note that a slug URL (`/<ping-key>/<slug>`) also works.

## What the design should change

1. **Prefetched state for Razorpay (Q2) — contradicts sweeper.md and sources.md.** A listed payment lacks its refunds, and the list's `expand[]=refunds` returns an empty collection even for refunded payments. Use the listed state as prefetched only when `amount_refunded == 0` and `refund_status` is null (list with `expand[]=card`). Otherwise enqueue without it and let the worker fetch. Refund jobs always fetch. The mapper refuses a state with no `refunds` whose `amount_refunded` > 0. This changes [sweeper.md](../design/sweeper.md) "Razorpay: polling" and "Manual backfill" and [sources.md](../design/sources.md) "Listing for verify and backfill", and qualifies F18's "nothing is fetched twice" for Razorpay.
2. **Never send `expand[]=refunds`, `expand[]=transaction` or a bracketless `expand=` on a list** (Q2). They return wrong data, are ignored, or give a 500 respectively.
3. **Always send both bounds** (Q1, Q3). A missing bound means unbounded, and refunds read a millisecond `to` as "no upper bound". The adapter's existing seconds-and-range check must cover `to` on refunds too. The hourly re-list gets an explicit `to = source_now − 60 s`.
4. **Don't use `status` on `/v1/refunds`** (Q3): it is validated and ignored. Correct razorpay-first Q3(f)'s wording if that note is touched.
5. **Paging stops at a short or empty page** (Q1): skip past the end is a 200 with no items.
6. **Heartbeat** (Q6): POST to the success URL once per sweep pass, with a 10 s timeout, no `/start` and no `/fail`. `doctor` checks the body is exactly `OK` and uses `/log`.
7. No change to the paging rule, the overlap, the re-list windows or the state hash: every hashed field is on the listed entities (Q2, Q3, Q5).

## API runs

Run 2026-10-03, 07:52-07:56 UTC, from the cloud session against `https://api.razorpay.com`. The owner's test key was sent as HTTP Basic from the environment, after the helper checked the `rzp_test_` prefix, and was never printed. **GET only, 124 calls.** Ids shortened to their last 6 characters. Payments: `…8h2378` 1790970020 failed; `…WcsMeE` 1790969938, `…d6kRqO` 1790969339 (2 refunds), `…EXjIdB` 1790956739 and `…MO3PEj` 1790956611, all captured; `…Ge5X07` 1790956586 and `…DwqGru` 1790956537, both created. Refunds: `…OulF3Q` 1790970053 and `…5WLvww` 1790970043, both processed, on `…d6kRqO`. Raw answers are in [tests/recordings/razorpay/api/list/](../../tests/recordings/razorpay/api/list/README.md).

| # | Call | Status | Seen |
|---|---|---|---|
| 1 | `/v1/payments?count=100`, `/v1/payments`, `?from=` | 200 | 7, strictly newest first |
| 2 | `/v1/payments?count=1&skip=0` / `skip=6` / `count=3&skip=2` | 200 | `…8h2378`; `…DwqGru`; `…d6kRqO, …EXjIdB, …MO3PEj` |
| 3 | `/v1/payments?count=1&skip=7`, `count=100&skip=7`, `skip=1000000` | 200 | `count: 0`, `items: []` |
| 4 | `/v1/payments?from=T&to=T`, T = 1790969938, 1790970020, 1790956537 | 200 | That one payment each: both bounds inclusive |
| 5 | `/v1/payments?from=1790969939&to=1790970019` | 200 | 0 |
| 6 | `/v1/payments?from=1790969938` / `to=1790969938` / `from=1790970021` / `to=1790956536` | 200 | 2 (≥ T) / 6 (≤ T) / 0 / 0 |
| 7 | `/v1/payments?from=1790970020&to=1790969938` | 200 | 0 |
| 8 | `/v1/payments?from` or `to` in ms, or `-1` | 400 | "from (to) must be between 946684800 and 4765046400" |
| 9 | `/v1/payments?from=1790969938.5` | 400 | "from must be an integer." |
| 10 | `/v1/payments?count=0`, `-1` / `101` / `abc`; `skip=-1` / `abc` | 400 | "…at least 1." / "…may not be greater than 100." / "…an integer."; "The skip must be at least 0." / "…an integer." (`field` set) |
| 11 | `/v1/payments?count=100&expand[]=card` | 200 | `card: null` on all (netbanking) |
| 12 | `/v1/payments?…&expand[]=refunds` (4 variants) | 200 | `refunds` **empty on every item**, including `…d6kRqO` |
| 13 | `/v1/payments/pay_…d6kRqO?expand[]=refunds&expand[]=card` | 200 | `refunds.count` 2, both processed |
| 14 | `/v1/payments?count=100&expand[]=transaction` / `transaction.settlement` / `settlement` | 200 | No key added |
| 15 | `/v1/payments?count=100&expand[]=emi` / `offers` / `token`; all six together | 200 | `emi`+`emi_plan` null; empty `offers`; `token` null; together as each alone |
| 16 | `/v1/payments?count=100&expand[]=bogus` | 400 | "Value of each expand must be one of following types: card, emi, transaction, transaction.settlement, refunds, offers, token, settlement" |
| 17 | `/v1/payments?count=100&expand=refunds` | **500** | `SERVER_ERROR`, `source: NA` |
| 18 | `/v1/payments?after`, `before`, `starting_after`, `ending_before`, `cursor`, `sort`, `order` | 400 | "<name> is/are not required and should not be sent" |
| 19 | `/v1/refunds?count=100`, `/v1/refunds` | 200 | 2, newest first; keys as `GET /v1/refunds/{id}` |
| 20 | `/v1/refunds?count=1&skip=0/1/2`, `count=100&skip=1000000` | 200 | `…OulF3Q`, `…5WLvww`, empty, empty |
| 21 | `/v1/refunds?from=T&to=T` (both); `from=1790970044&to=1790970052` | 200 | One each; 0 |
| 22 | `/v1/refunds?from=1790970053` / `to=1790970043` / `from=1790970054` / `from>to` | 200 | 1 / 1 / 0 / 0 |
| 23 | `/v1/refunds?from` in ms / `to` in ms / both in ms | 200 | 0 / **2** / 0 |
| 24 | `/v1/refunds?from=-1` / `to=-1` / decimal / `abc` | 400 | "The from (to) must be no less than 0." / "The from must be an integer." |
| 25 | `/v1/refunds?count=0` / `-1` / `101`; `skip=-1` | 400 | "…at least 1." / "…no less than 1." / "…no greater than 100."; "The skip must be no less than 0." |
| 26 | `/v1/refunds?payment_id=` `…d6kRqO` / `…WcsMeE` / `pay_00000000000000`; with `from=to=1790970053` | 200 | 2 / 0 / 0; 1 |
| 27 | `/v1/refunds?status=pending` / `failed` / `processed`; `status=created` | 200; 400 | **2 each (not filtered)**; "The status must be a valid value." |
| 28 | `/v1/refunds?expand[]=payment`, `starting_after=…`, `after=…` | 200 | Ignored |
| 29 | `/v1/refunds/rfnd_…OulF3Q` | 200 | Same keys as a listed refund |
| 30 | 10 × payments list, 5 × refunds list sequential; 20 × payments list on 10 threads | 200 all | 1.46-1.83 s; 0.79-1.04 s; 1.44-1.79 s. No rate-limit headers |

## Sources

- **Razorpay docs** (each read as `<url>.md`; searched through https://razorpay-881012b3.mintlify.site/llms-full.txt): https://razorpay.com/docs/api/payments/fetch-all-payments/, https://razorpay.com/docs/api/payments/fetch-all-payments-with-expanded-card-details/, https://razorpay.com/docs/api/refunds/fetch-all/, https://razorpay.com/docs/api/refunds/fetch-multiple-refund-payment/, https://razorpay.com/docs/api/refunds/entity/, https://razorpay.com/docs/api/payments/entity/, https://razorpay.com/docs/api/understand/ (Rate Limiting, Pagination), https://razorpay.com/docs/api/x/ (Rate Limits). `…/fetch-all-payments-with-expanded-emi/` and `…-upi/` don't exist (404).
- **Razorpay source:** razorpay-python `razorpay/resources/refund.py` (master, read 2026-10-03); razorpay-mcp-server `pkg/razorpay/refunds.go` (main, read 2026-10-03); this repo's `src/webhook_sync/sources/razorpay.py` (`_processed_refunds`).
- **healthchecks.io:** https://healthchecks.io/docs/http_api/, /docs/reliability_tips/, /docs/python/, /docs/bash/, /docs/measuring_script_run_time/, /docs/configuring_checks/, /docs/monitoring_cron_jobs/; source https://github.com/healthchecks/healthchecks at 3addc67, `hc/api/views.py` (`ping`, `ping_by_slug`).
- **Earlier notes cited:** [razorpay-first.md](razorpay-first.md) (Q3, Q7, S-Q3a), [razorpay-fetch-facts.md](razorpay-fetch-facts.md) (Q1, Q2, Q4, Q5, Q8), [gaps-google-python-razorpay.md](gaps-google-python-razorpay.md) (G32, G39), [research-gaps.md](../design/research-gaps.md) (G50, G53, G56).
- **Runs, 2026-10-03:** api.razorpay.com with the test key, as listed above. No healthchecks.io ping was made.
- **Not tried:** any write (so no drift under a concurrent create, no pending refund, no same-second tie); a card payment, so `expand[]=card` on the list was seen only as `null`; a page of 100 real items; a 429.
