# Design audit: webhook sync

**Date:** 2026-09-30. **Reviewer:** independent senior review, adversarial. **Scope:** `CONTEXT.md`; everything in `docs/design/` (overview, 12 part pages, `edge-cases.md`, 5 ADRs, `grilling-log.md` Q1–Q65); `docs/research/feature-inventory.md`; the vault background (`research/demo-baselines/00-summary.md` and `02-webhook-sync.md`, `domain/plan.md`, `domain/market-analysis.md` sections 1–7, the API-glue and spreadsheet sections of `research/agent-reports/07-sub-api-glue-pdf-spreadsheets.md` and `01-categories-market-map.md`, and the tail of `session-log.md`). Nothing is built, so every finding is about the design as written.

**How claims were checked.**
- **Local experiments** on this machine (Windows 11, CPython 3.14.7, SQLite 3.53.1, SQLAlchemy 2.1.1 async with aiosqlite, psycopg 3 binary, uvicorn 0.54.0, stripe-python 15.6.1). Scripts are in the session scratchpad (`audit-lab/exp1_queue.py` to `exp4_manual_begin.py`). Results are quoted where used.
- **Primary docs** for Stripe, Razorpay, Google Sheets, Google Cloud and GitHub, fetched 2026-09-29/30. URLs are in the Sources section.
- **Code** in `D:\second-brain\repo-study\webhook\` (sync-engine README and package list; razorpay-python mocks and settlement docs).
- **Market and competitor claims** come from vendor pages. Where a price or feature comes from a vendor's own page or a competitor's comparison page, the text says so.
- No real Stripe, Razorpay or Google API was called with credentials. Claims that need a live account (for example, whether a Stripe resend is byte-identical) are marked **unverified** with a test to run.

---

## Executive summary

1. The design is careful and mostly well reasoned. But the queue as written **gets stuck on ordinary traffic**. A job that fails, is reaped or is released goes back to `waiting` while a follow-up job for the same payment is already waiting. That breaks the partial unique index. The local test shows the reaper's single `UPDATE` then fails for *every* expired job, so nothing is ever reaped again (F01).
2. Three core guarantees rest on assumptions the fakes would share and real APIs break:
   - "Conflict" by raw-body hash will fire on nearly every event, because the sweeper's copy of an event never has the delivery's bytes (F02).
   - The sweeper's "oldest first with a per-page checkpoint" is not what `created[gte]` gives you. Implemented the obvious way, it can skip pages after a crash, and it stalls on a quiet account (F03).
   - The post-write key check cannot see a mid-flush sort if the key cell is part of the write, and the probe resumes the sink anyway (F05).
   The fakes are written from the same beliefs, so CI would stay green (F21).
3. **The schedule does not fit.** Slices 1–6 are about 80–120 hours. After pitching, demo 1 and demo 3, Lalit has roughly 4–8 build hours a week. That puts pitch-ready between mid-December and April (late November at the very best), against pitching from 8 October. A cut of about 25 hours gives the same core video (F30).
4. **The commercial story is weaker than the engineering.** The one real Stripe-webhook post in the vault ($120) is about subscriptions syncing to Supabase. v1 excludes both subscriptions and "a client's own database". A Stripe → Sheets payments log is already sold for $0–35 a month, and Stripe's own open-source sync engine has a Google Sheets destination. Buyers who look at a payments sheet ask first for fees, net, product and order id. v1 has none of them, though fee and net are one `expand` away (F31, F32).
5. **Nobody is told when the service itself is down.** Every alert comes from inside the process. `/ready` also fails when a source is paused, which makes orchestrators restart a healthy receiver. Hosting, monthly cost, TLS, upgrades, backups off the box, and what happens after 30 April 2027 are undecided (F27, F28).
6. **SQLite through SQLAlchemy async needs a deliberate pattern.** The documented "BEGIN IMMEDIATE on every begin" recipe also serialises admin reads (a read waited 1.9 s in the test). A pool plus `busy_timeout` produced 39 "database is locked" failures (503s) in a 1,000-delivery storm. With `synchronous=FULL`, p99 latency was 5.5–6 s, over Razorpay's 5-second timeout (F14, F15).
7. Smaller correctness gaps:
   - With `--workers N` or two processes, a stale fetch can overwrite a newer one (F04).
   - A crash during rebuild leaves the flusher writing to the old tab while verify reports 0 differences (F06).
   - Razorpay's event id is outside the signature, so a captured delivery can be replayed under fresh ids (F12).
   - Razorpay polling by creation time misses changes to older payments (F13).
   - Payload mode has three contradictory rules for a case (404) that mostly means deleted test data (F09).

**Top 10 changes, ranked:**
1. **One live job per (stage, sink, payment)**, with a `rerun` flag set by enqueue while the job runs, as driftless does. This removes the index collision (F01) and the concurrent-fetch race (F04) in one change. About 3 h.
2. **Cut pitch-ready to about 25 h.** Keep: Stripe, SQLite, the flusher, the sweeper, `wsync verify`, three scenes. Defer: Postgres, `--workers`, Razorpay, admin pages beyond one, rebuild-by-swap, payload mode, pause/probe machinery, the rejections table, Hypothesis suites. See the Schedule section (F30, F34).
3. **Add what a sheet buyer asks for first**, and pitch to the buyers who post:
   - fee, net and settlement-currency amount (one `expand`);
   - payment method, metadata keys, the Checkout order id;
   - a daily summary tab;
   - a Postgres/Supabase sink plus subscription and invoice events as the first paid adapter (F31, F32).
4. **Define a conflict by meaning, not bytes:** a different `(type, data.object.id, created)`. The sweeper never compares bytes. Test resend byte-equality with the Stripe CLI in slice 0 (F02).
5. **Rewrite the sweep as time slices.** Walk each slice fully in any order, then commit `checkpoint = slice end`. Slice ends come from Stripe's clock minus a grace period. Overlap 10 minutes, as driftless does. Advance on empty slices (F03).
6. **Make the Payments tab machine-owned.**
   - Never write the key cell on update.
   - Repair in place by key instead of swapping tabs, so buyer columns stay attached.
   - Append after the last used row.
   - Give buyers a view tab (`=SORT(FILTER(…))`) and filter views for sorting.
   - A pause that needs a person is never auto-resumed by a header probe (F05–F08).
7. **Test against reality once a week.** Record real Stripe deliveries, resends, `/v1/events` pages and real Sheets responses into fixtures in slice 0. Run a scheduled GitHub Action with a Stripe *test-mode* restricted key and a throwaway sheet. Crash points kill a subprocess (F21, F22).
8. **External monitoring and an honest `/ready`.** A dead-man's switch pinged by the sweeper, an uptime check on `/health`, readiness that ignores source pauses, and a written hosting and handover plan with monthly cost (F27, F28).
9. **One SQLite writer connection behind an asyncio lock**, a read-only engine for pages and verify, `synchronous=FULL` kept, rejections counted in memory. Load checks assert "2xx or 503, then exactly one event" (F14, F15).
10. **Least-privilege keys.** Require a Stripe restricted read-only key in live mode, check each permission in `doctor`, and make a 403 on a secondary call degrade one column instead of pausing the source. For Razorpay, add a unique body hash (F12, F23).

---

## Findings table

| ID | Sev. | Area | Title |
|---|---|---|---|
| F01 | Critical | Queue | Fail, reap and release collide with the coalescing index; one collision stops the reaper for every job |
| F02 | High | Receiver / sweeper | "Conflict" by raw-body hash fires on normal traffic, including swept events and possibly every resend |
| F03 | High | Sweeper | The sweep's order and checkpoint can't be built as written; the obvious build loses events; quiet accounts stall |
| F04 | High | Ledger worker | Two jobs for one payment can run at once, and an older fetch can overwrite a newer one |
| F05 | High | Sheets sink | The post-write key check can't catch the sort it was added for, and the probe resumes anyway |
| F06 | High | Sheets sink | Rebuild detaches buyer columns; a crash after the swap makes the flusher write to the old tab, and verify agrees with it |
| F07 | Medium | Sheets sink | Where new rows go is unspecified; both readings fail (1,000-row gap, or overwriting a buyer's total row) |
| F08 | Medium | Sheets sink | Reads by tab title, 400s from sheet state classed `dead`, probe blind to the pause reason |
| F09 | Medium | Ledger worker | Payload mode has three contradictory rules, a guard fetches never advance, and little reason to exist |
| F10 | Medium | Sources / mapping | Business values are mapped wrongly in several common cases |
| F11 | Medium | Sheets sink / ops | Singleton leases have no heartbeat, and the sheet can't check a fencing token |
| F12 | Medium | Receiver / security | Razorpay's event id is unsigned: replays under fresh ids are new events |
| F13 | Medium | Sweeper (Razorpay) | Polling by creation time misses captures, disputes and refund changes on older payments |
| F14 | High | Database | BEGIN IMMEDIATE through SQLAlchemy async needs a chosen pattern; the pool plus busy_timeout gives 503s |
| F15 | Medium | Database / load | `synchronous=FULL` throughput makes the storm test flaky and exceeds Razorpay's timeout; rejections amplify floods |
| F16 | Low | Platform | Postgres async doesn't run under uvicorn on Windows; CI Postgres is Linux-only |
| F17 | Low | Queue | Smaller queue gaps: instance id, traceability of coalesced events, lease clock |
| F18 | Medium | Source APIs | Stripe's read-request allocation is ignored; probes, backfill and "quick" verify cost more than stated |
| F19 | Medium | Sheets / load | "≤10 Sheets calls for 200 events" is false once the third call and the fetch rate limit are counted |
| F20 | Low | Facts | Fact-check of external API claims: mostly right, five corrections |
| F21 | High | Testing | The fakes encode the same assumptions as the design; the biggest bugs here would pass CI |
| F22 | Medium | Testing | Invariants and crash points miss the failure modes that matter |
| F23 | Medium | Security | Full secret keys allowed; a 403 on a secondary call pauses the whole source |
| F24 | Medium | Security | Admin login shares the public host, with one token and no throttling |
| F25 | Medium | Privacy | Personal data in payloads, events and backups; DPDP and GDPR roles unaddressed |
| F26 | Low | Operations | Google service-account keys are blocked by default in newer Google Cloud organisations |
| F27 | High | Operations | No alert when the process or host is down; `/ready` fails on a paused source and invites restarts |
| F28 | High | Operations | Hosting, cost, TLS, upgrades, off-box backups, support and the April 2027 end are undecided |
| F29 | Medium | Operations | `doctor` doesn't check the Stripe endpoint's subscribed event types or API version |
| F30 | Critical | Schedule | Slices 1–6 are 80–120 h against 4–8 build hours a week; pitching starts 8 October |
| F31 | High | Product | The buyers who post want app-state sync and subscriptions; the Sheets payments log is commoditised |
| F32 | High | Product | The sheet lacks fees, net, product, order id and payment method |
| F33 | Medium | Sheets sink | Dates written as text break date filters, pivots and monthly totals |
| F34 | Medium | Scope | Over-engineering for a solo demo |
| F35 | Medium | Consistency | Contradictions between pages, ADRs and the grilling log |
| F36 | Medium | Showcase | Three of the five scenes can't be shot as written |
| F37 | Low | Consistency | Glossary slips and stale status lines |
| F38 | Low | Verify | "Every night it proves your sheet matches Stripe" is wider than what verify checks |

**Counts:** 2 Critical, 11 High, 19 Medium, 6 Low.

---

## Findings in detail

### A. Correctness of the guarantees

#### F01: Critical: Fail, reap and release collide with the coalescing index; one collision stops the reaper for every job

**What the design says.**
- `data-model.md:114`: "A partial unique index on `(stage, sink, source, payment_key) WHERE status = 'waiting'` … A running job does not block it, so a new event during a run creates exactly one follow-up."
- `queue.md:20–24`: `running --> waiting : fail, class retry`, `running --> waiting : reaped`, `running --> waiting : released at shutdown`, `running --> waiting : fail, class pause`.
- `queue.md:70`: "**reap().** Every 30 s: jobs with `status = 'running' AND lease_until < now` go back to `waiting`".

**Why it's wrong.** A running job for payment P plus a waiting follow-up for P is the normal state the design aims for. Every path that moves the running job back to `waiting` then creates a second waiting row for P, which the index forbids.

Local test (`exp1_queue.py`, SQLite 3.53.1 through SQLAlchemy async and aiosqlite):
```
fail(retry) raised: IntegrityError … UNIQUE constraint failed: jobs.stage, jobs.sink, jobs.source, jobs.payment_key
reaper raised: IntegrityError … UNIQUE constraint failed: …
after reaper attempt: [(1, 'pi_1', 'running'), (2, 'pi_1', 'waiting'), (3, 'pi_2', 'running')]
```
Job 3 (a different payment, lease expired) was never reaped, because the reaper is one `UPDATE` over all expired rows and the whole statement fails. Postgres raises 23505 for the same statement (not run here: no server).

**Failure timeline (the flusher, where it is most likely).**
1. 10:00:00. The flusher claims 200 mirror jobs.
2. 10:00:01. Ledger updates for 30 of those payments enqueue 30 new waiting mirror jobs. This is normal during a burst.
3. 10:00:02. Sheets answers 429. The flusher calls `fail(retry)` on all 200. The 30 with a waiting twin raise `IntegrityError`. Depending on the code, either the other 170 fail too, or those 30 stay `running`.
4. 10:01:02. Their leases expire. The reaper's `UPDATE` hits the same collision and aborts.
5. From then on **no job of any kind is ever reaped**. Invariant 4 ("Nothing is stuck") is broken for good, and a job whose worker crashes stays `running` forever. `release` at shutdown and `fail(pause)` hit the same wall.

This is driftless's "retry all" bug (feature inventory, bug 3) in another place. The design fixed it only for the dead-letter retry (`queue.md:136`).

**Recommendation.** Pick one:
- **(a) One live job per key (recommended).** Make the unique index `WHERE status IN ('waiting','running')`. `enqueue` on a running job sets `rerun = 1` (and bumps `caused_by_event_id`). `complete` sets `status = 'waiting', attempts = 0, run_after = now` when `rerun = 1`, otherwise `done`. Fail, reap and release then never collide, because no second live row exists. This is driftless's `CompleteJob` requeue, which the inventory marked **take**. It also serialises work per payment, which fixes F04.
- **(b) Keep two rows and supersede.** In fail, reap and release: if a waiting twin exists, mark this job `superseded`, and push the twin's `run_after` to at least this job's backoff and its `attempts` to at least this job's. Otherwise a poison payment escapes the dead-letter queue by always having a fresh twin. The reaper must process rows one at a time, or with `NOT EXISTS` in its `WHERE`.

Add a property test: "a waiting twin exists while the running job fails, is reaped or is released".

**Effort.** 2–3 h (design text, SQL and tests).

---

#### F02: High: "Conflict" by raw-body hash fires on normal traffic, including swept events and possibly every resend

**What the design says.**
- `CONTEXT.md:48–49`: "**Conflict**: A delivery whose dedupe key is already stored with a different body … logged for a person to look at."
- `receiver.md:70`: "not inserted: compare body_sha256 with the stored one → duplicate or conflict".
- `sweeper.md:32`: "store through the receiver's transaction (ON CONFLICT DO NOTHING), origin = 'sweep'".
- `operations.md:85`: a **conflict** alert fires on "a delivery with a known id and a different body".

**Why it's wrong.** Several legitimate copies of one Stripe event have different bytes:
1. **Sweeper vs delivery.** The sweeper holds parsed JSON from `GET /v1/events`, so any body it stores is a re-serialisation. Key order, whitespace and escaping differ from the delivered bytes. Stripe also renders the two differently. The API: "Each event data is rendered according to Stripe API version at its creation time". The webhook: "The Event sent to the destination is structured for the event destination's specified version". If the endpoint's version differs from the account default, even `data` differs.
2. **Top-level fields change.** The Event object carries `pending_webhooks`. It is a count, so it is presumably lower after deliveries succeed. A resend or a swept copy made later would then differ from the first delivery. **Unverified:** whether retries and `stripe events resend` produce byte-identical bodies. Stripe documents only that a retry gets "a new signature and timestamp".

**Failure timeline.**
- 10:00:00. `payment_intent.succeeded` (evt_A) is delivered and stored with hash H1.
- 10:10:00. The sweep re-reads evt_A from `/v1/events`, since it lists *all* events and not only undelivered ones (`sweeper.md:39`). The hash is H2 ≠ H1, so it is logged as a conflict and alerts.
- The same happens for every event, every 10 minutes. The conflict counter tracks total traffic, the alert fires hourly forever, and a real conflict becomes invisible.
- Scene 1 (`stripe events resend` ×5) may show "conflicts 4" and a red alert instead of "duplicates 0 → 4".

If the sweeper reuses the receiver's transaction as written (`sweeper.md:32`), it also writes `deliveries` rows for things that were not HTTP requests, which inflates "duplicates" on the admin page.

**Recommendation.**
- Store `body_sha256` for information only.
- Define a conflict as a different **semantic fingerprint**: `sha256(type, data.object.id, created, livemode)` for Stripe, and `event, entity id, created_at` for Razorpay. That is what "same id, different event" means.
- The sweeper uses insert-if-absent with no comparison and no `deliveries` row. Record the sweep's own count in `checkpoints`.
- In slice 0, run `stripe listen`, trigger one payment, run `stripe events resend` twice, and diff the three bodies. Keep the result as a fixture.

**Effort.** 1–2 h.

---

#### F03: High: The sweep's order and checkpoint can't be built as written; the obvious build loses events; quiet accounts stall

**What the design says.**
- `sources.md:80`: "`GET /v1/events?types[]=…&created[gte]=<checkpoint − 60 s>`, auto-paginated oldest first, 100 a page".
- `sweeper.md:33`: "same transaction: checkpoint = newest created on the page".
- `sweeper.md:40`: "An event can appear in the list a little after its `created` time. Re-reading the last minute costs a few duplicate candidates".
- `sweeper.md:27–30`: past 25 days since the checkpoint, alert; past 30, a full backfill.

**Why it's wrong.**
1. **Order.** Stripe lists newest first. Stripe's own guide: "Using ending_before with auto-pagination returns events in chronological order." With only `created[gte]`, the first page is the *newest* 100 events in the window.
2. **The obvious build loses events.** Page 1 (newest) commits `checkpoint = newest created`. The process crashes before page 2. The restart lists from `newest − 60 s`, and the older pages are never read. Any of those events that was also never delivered is **lost silently**. This contradicts the guarantee "events missed while down are recovered automatically".
3. **The correct oldest-first build needs an id cursor** (`ending_before=<last event id>`), not a timestamp. A cursor cannot express "re-read the last minute". An event that becomes listable late but sorts before the cursor is skipped.
4. **Quiet accounts stall.** "Newest created seen" doesn't move when there are no events. A seasonal buyer with no payments for 25 days gets a "horizon near" alert. At 30 days a full PaymentIntent backfill runs, with nothing missing.
5. **The overlap is a guess.** The design shrank driftless's 10-minute overlap (feature inventory, sweeper row: "`[last done sweep.window_to − overlap(10 m), now − grace(60 s)]`") to 60 s, with no source.

**Recommendation.** Sweep in **time slices**:
- `end = min(start + 6 h, stripe_now − 60 s)`, where `stripe_now` comes from the `Date` header of the first list response.
- Walk the whole slice with `created[gte]=start−10 min, created[lt]=end` in whatever order Stripe returns, inserting each event.
- Then commit `checkpoint = end` in its own transaction. Empty slices advance too.
- A crash re-walks at most one slice, and dedupe makes that free.
- The horizon check compares `stripe_now − checkpoint`. That is now small on quiet accounts.

Update H3 and the `mid_sweep_page` crash point: "mid-slice crash re-walks the slice".

**Effort.** 2–3 h.

---

#### F04: High: Two jobs for one payment can run at once, and an older fetch can overwrite a newer one

**What the design says.**
- `data-model.md:114`: a waiting job may exist while one is running.
- `ledger-worker.md:35`: "the fetch happens first, with no transaction open".
- `operations.md:24`: "On Postgres, `wsync serve --workers N` adds ledger workers".
- `edge-cases.md:152` (I9): two processes on one SQLite file are covered by "one writer at a time … fenced claims".

**Why it's wrong.** Fencing stops a stale *completion*, not a stale *write by a job that still holds its lease*. Stripe PaymentIntents, Charges and Disputes carry no `updated` field, so the ledger cannot order two fetched states.

**Failure timeline (Postgres, `--workers 2`; or two processes on SQLite).**
1. Worker A claims job 7 for P and fetches at 10:00:00.000: `succeeded`, refunded 0.
2. 10:00:00.300. A refund is created. `refund.created` enqueues job 8 for P (A's job is running, so a new waiting row appears).
3. Worker B claims job 8, fetches `partially_refunded` and commits at 10:00:00.900.
4. Worker A's commit is slow (GC, a lock wait). At 10:00:01.200 it upserts `succeeded`, refunded 0. The row hash differs, so it writes and enqueues a mirror job. Its token is valid, so `complete` succeeds.
5. The ledger and the sheet now show no refund. No job is pending for P. The row stays wrong until the next event for P or nightly verify.

The race test (`testing.md:82`: "no payment written by two workers at once") checks *simultaneous* writes, not *order*. It would pass.

**Recommendation.** F01 option (a) fixes this: one live job per payment means one fetch at a time per payment. If option (b) is chosen, the claim needs `AND NOT EXISTS (SELECT 1 FROM jobs r WHERE r.status='running' AND r.stage=j.stage AND r.sink=j.sink AND r.payment_key=j.payment_key)`. Add invariant 5 (F22): "no row ever regresses to an older source state".

**Effort.** Included in F01. Otherwise 1 h.

---

#### F05: High: The post-write key check can't catch the sort it was added for, and the probe resumes anyway

**What the design says.**
- `sheets-sink.md:52`: "`updateCells` for every changed and new row".
- `sheets-sink.md:54`: "re-read the key cells of the rows just written. If a buyer sorted or moved rows between our read and our write … the sink pauses with 'sheet changed during write'".
- `queue.md:126`: "Every 60 s, one **probe** runs: for Sheets, reading the header row. On success the sink resumes".
- `sheets-sink.md:120`: developer metadata as a "later option".

**Why it's wrong.**
1. **If the update writes the whole row, the key cell included, the check always passes.** Timeline:
   - The flusher reads: row 7 = X.
   - The buyer sorts: row 7 is now Y, with Y's own notes in the columns to the right.
   - The flusher writes X's full row, key included, into row 7.
   - The check reads row 7 and finds X, "in place". It passes.

   Result: Y's row is destroyed, X appears twice, and **the buyer's notes for Y now sit next to X's data**. Nothing alerts until nightly verify reports a duplicate (no automatic repair) and a missing row.
2. **If the key cell is excluded**, the check detects the move, but only after X's amount and status have been written onto Y's row.
3. **The probe then resumes within 60 s**, because the header is fine. The wrong row stays until Y next changes or verify runs. The admin page's "Rebuild" offer is never needed to resume.
4. **Developer metadata doesn't scale.** Google allows 30,000 characters of developer metadata per sheet. At about 25–30 characters per row that is roughly 1,000 rows, so it is not a general fix.

**Recommendation.**
- Never write the key cell on update. Only appended rows get a key.
- On a detected move, **repair immediately** instead of pausing: enqueue mirror jobs for X and for every key found at the written rows. The next flush re-maps by key and rewrites both rows.
- Make the probe reason-specific (F08). "Sheet changed during write" never auto-resumes if repair fails twice.
- **Remove the incentive to sort the managed tab.** The header note tells buyers to sort with **filter views**, which sort only the viewer's view. Ship a "View" tab with `=SORT(FILTER(Payments!A2:M, …))`. Say plainly that sorting the Payments tab itself is supported but slower to settle.

**Effort.** 2 h.

---

#### F06: High: Rebuild detaches buyer columns; a crash after the swap makes the flusher write to the old tab, and verify agrees with it

**What the design says.**
- `sheets-sink.md:18`: "Add your own columns to the right."
- `sheets-sink.md:94–95`: rebuild writes every row to a new tab, then "ONE batchUpdate: rename old → … rename new → 'Payments'", then "store the new tab id; resume the sink".
- `sheets-sink.md:98`: "their old tab, with any columns they added, stays until they delete it."
- `testing.md:72`: the only rebuild crash point is `mid_rebuild`, "new tab half-written".

**Why it's wrong.**
1. **Buyer columns.** A buyer who uses the sheet for work (shipped, invoiced, notes) loses those columns from the live tab on every rebuild. Rebuild is offered for "layout changed", which is usually a buyer inserting a column, and for "sheet changed during write". Moving notes back by `VLOOKUP` is manual work the design pushes onto the buyer.
2. **Crash between the swap and storing the id.**
   - The swap `batchUpdate` succeeds.
   - The process dies before the database write "store the new tab id".
   - On restart the flusher uses the stored **old** id. Tabs are followed by id, so it writes happily into "Payments (old 2026-09-30 14:05)".
   - The buyer's "Payments" tab stops changing.
   - Verify reads the tab by the stored id, finds the old tab consistent with the ledger, and reports **0 differences**.
   - This is silent divergence of exactly the kind the product sells against.

**Recommendation.**
- Make **repair in place** the default: rewrite the header, rewrite our columns by key, append missing rows, report extras. Buyer columns stay attached to their rows.
- Keep tab-swap only for "the tab is gone or unreadable".
- If swap stays: write `pending_tab_id` before the swap. At startup, resolve the live tab by a **sheet-level** developer-metadata tag (`webhook-sync:role=live`, one entry per sheet, well within limits), not by a stored id alone.
- Add crash point `after_swap_before_store`.

**Effort.** 3–4 h, which replaces most of the swap work.

---

#### F07: Medium: Where new rows go is unspecified; both readings fail

**What the design says.** `sheets-sink.md:52`: "`appendDimension` for as many rows as are new, then `updateCells` for every changed and new row". `sheets-sink.md:50`: "Rows whose key isn't ours (a buyer's total row, a note) are left alone."

**Why it's risky.**
- **Reading 1: write new rows at the old grid end.** A buyer-created spreadsheet has a 1,000-row grid, so the first payment lands on row 1,001, below 999 blank rows.
- **Reading 2: write after the last key.** A buyer's total row with column A empty (`=SUM(E2:E50)` in E51) is not visible in the key column. The next new payment is written over row 51, destroying the buyer's formula.

The design reads only "tab properties + header row + key column" (`sheets-sink.md:47`), so neither the last used row nor buyer content below the data is known.

**Recommendation.**
- Read the used range of the whole tab in the same call. A values read of `A:ZZ` returns rows up to the last non-empty row in any column.
- Append after the last used row. Call `appendDimension` only when the grid is too small.
- On first setup, trim the grid to header + 1 row.
- Tell buyers to put totals above the header or in the summary tab.

**Effort.** 1 h.

---

#### F08: Medium: Reads by tab title, 400s from sheet state classed `dead`, probe blind to the pause reason

**What the design says.**
- `sheets-sink.md:47`: "**Read once** with `spreadsheets.get` and a field mask: the tab's id, title and grid size, the header row, and the key column".
- `sheets-sink.md:49`: "The tab is found by its numeric id, so a renamed tab is followed".
- `sheets-sink.md:114`: "400 from a bad request we built … That mirror job goes `dead`".
- `sheets-sink.md:32`: "abort: fail every claimed job (class from the error)".

**Why it's wrong.**
1. **`spreadsheets.get` `ranges` are A1 notation**, which means tab titles: "Ranges are specified using A1 notation … Sheet2!A1:C4". Reading the header and key column of the tab *with id N* in one call needs `spreadsheets.getByDataFilter` with a `gridRange` (sheetId). Otherwise, after a rename the cached title gives 400 "Unable to parse range". The failure table classes 400 as `dead`, so all 200 claimed jobs die on a tab rename. That contradicts G8.
2. **A tab deleted between read and write** gives a 400 on `updateCells` (no grid with that id). The design bisects it as "the one bad row". Every half fails, and every job goes dead.
3. **The probe ignores the reason.** For "sheet full", "another deployment writes here" and "sheet changed during write", reading the header succeeds. The sink resumes, fails again, and pauses again, flapping every minute. Alerts are limited to one per kind per hour, which hides the flapping.

**Recommendation.**
- Read with `getByDataFilter` (gridRange by sheetId), or make two calls (`fields=sheets.properties`, then values by the current title).
- Class a 400 whose message names a sheet id, grid or range as `pause` (layout). Bisect only when the error names a request index with a cell value.
- Give each pause reason its own probe: the header for layout, the cell count for "full", the Sync status owner for "another deployment". Some reasons (changed during write) require **Resume** or a completed repair.

**Effort.** 2 h.

---

#### F09: Medium: Payload mode has three contradictory rules, a guard fetches never advance, and little reason to exist

**What the design says.**
- `sources.md:50`: a payload never moves a row out of `canceled` or backwards "**unless** its `(created, event_id)` is newer than the row's `payload_version`".
- `ledger-worker.md:45`: "it is written only if that pair is newer than the row's `payload_version`, **and** never moves the row out of `canceled` or backwards".
- `ADR 0002:11`: "When a fetch fails permanently (**404, revoked key**), the event payload is written".
- `queue.md:103` and edge F4: payload mode "is only for 404".
- `CONTEXT.md:83`: "an event that would move a row backwards is ignored".

**Why it's wrong.**
1. "Unless" and "and" are opposite rules. The ADR's "revoked key" contradicts the queue page.
2. "Backwards along the status order" is undefined: the order has cycles (`failed → processing`, `refunded → partially_refunded`, `sources.md:33,44`).
3. **Fetch writes never set `payload_version`.** A payload can therefore overwrite a *newer fetched* state. Timeline: a fetch writes `refunded` (payload_version NULL). A later job gets 404, and the newest stored event is `payment_intent.succeeded`. It is "newer than NULL", so it is written: the refund disappears.
4. **When does 404 happen?** `ledger-worker.md:49`: "usually means test data was deleted at the source, or the key belongs to another account". In both cases, writing old payloads is the wrong move: a person should look.
5. `from_payload` for a `refund.created` event has no payment amount, so "the newest stored event" may not map to a row at all.

**Recommendation.** **Cut payload mode from Stripe v1.**
- On 404, keep the last fetched values, set `status_at_source = 'not_found'`, flag the row, and report it in verify.
- This deletes `payload_version`, `from_payload`, the stickiness rules and three contradictory texts.
- Design payload mode properly when the Standard Webhooks source is built. That source has no API, so payload mode is its only mode.

**Effort.** Saves 3–5 h of build. 0.5 h to rewrite the pages.

---

#### F10: Medium: Business values are mapped wrongly in several common cases

**What the design says.**
- `sources.md:76`: "`amount_minor` = `amount`".
- `data-model.md:86`: "`dispute_status` | `NULL`, `open`, `won`, `lost`".
- `sources.md:97` (Razorpay): "Dispute status comes from `GET /v1/disputes/{dispute_id}` when the event names a dispute".
- `data-model.md:111`: `caused_by_event_id` is "The latest event folded into this job".
- `sources.md:118`: `payment_intent.created` produces a `pending` row.

**Why it's wrong.**
- **Partial capture.** For `requires_capture` → capture less, `amount_received < amount`. The sheet's Amount column overstates revenue. Verify uses the same mapping, so it reports 0 differences.
- **Dispute statuses.** Stripe has eight: `warning_needs_response`, `warning_under_review`, `warning_closed`, `needs_response`, `under_review`, `won`, `lost`, `prevented`. The design maps to three and says nothing about the other five. An inquiry (`warning_*`) is not a chargeback; `prevented` is not `open`.
- **Razorpay dispute lost by coalescing.** A dispute event and a later refund event fold into one job. `caused_by_event_id` is the refund, so the job fetches only `/v1/payments/{id}`, which carries no dispute. The row is rebuilt with `dispute_status = NULL`, and the dispute disappears from the sheet.
- **Pending rows in sums.** With `payment_intent.created` handled, abandoned checkouts and every failed attempt get rows with amounts. A buyer who types `=SUM(E:E)` gets wrong revenue. That is the first thing most buyers do.

**Recommendation.**
- Show `amount_received` (and `amount` as "Authorised" when they differ).
- Map disputes to `inquiry`, `needs_response`, `under_review`, `won`, `lost`, `prevented`, `closed`, keeping `source_status`.
- For Razorpay, never overwrite `dispute_status` from a payment fetch. List disputes for the payment on every fetch, or keep dispute state in its own table.
- Don't make a row until `processing`, `succeeded`, `failed` or `requires_capture`, or put a "Revenue" column that is 0 unless succeeded. Ship the summary tab (F32) so buyers never need to SUM raw rows.

**Effort.** 2–3 h.

---

#### F11: Medium: Singleton leases have no heartbeat, and the sheet can't check a fencing token

**What the design says.**
- `operations.md:24`: "★ = singleton, held by a lease in `singletons`, so a second process started by mistake can't run it twice."
- `sheets-sink.md:89`: "Rebuild runs under the flusher's lease".
- `edge-cases.md:122`, G13: "the flusher lock lets one run, the other skips."

**Why it's risky.** The singleton lease length and its renewal are not specified. A flush makes up to three calls with 30 s timeouts plus bisection; a rebuild takes minutes. Fencing works for rows in `jobs`, but Google Sheets can't check a token, so a stalled holder's late write lands anyway.

Timeline, two processes:
1. P1 claims jobs, reads (next free row = 51), then stalls 70 s (laptop sleep, a stuck DNS lookup).
2. P1's singleton lease expires. P2 becomes flusher and writes payments Q and R at rows 51–52.
3. P1 wakes and writes P at row 51, its stale position. Q's row is gone, and P's check reads P "in place".
4. Verify reports Q missing the next night.

**Recommendation.**
- Prevent the second process instead of racing it: take an **OS-level exclusive lock** on a lock file next to the database at startup, and refuse to start if it is held.
- Heartbeat singleton leases.
- The flusher re-checks its lease immediately before the write, and skips if under 30 s remain.
- State the lease lengths in `queue.md`.

**Effort.** 1–2 h.

---

#### F12: Medium: Razorpay's event id is unsigned: replays under fresh ids are new events

**What the design says.**
- `sources.md:88–90`: the signature is "HMAC-SHA256 over the raw body"; the event id is "The `x-razorpay-event-id` **header**"; "The dedupe key is the only defence against a captured request being replayed".
- `edge-cases.md:161` (J3): "old Razorpay delivery replayed (no timestamp to check) → Same id: duplicate, 200."

**Why it's wrong.** The header is outside the signature. Anyone who has one real delivery (from a proxy log, a tunnel inspector, or a forwarding tool) can resend it with any `x-razorpay-event-id`. Every resend verifies and gets a new dedupe key, a new `events` row (payload kept 90 days, row kept forever), a `deliveries` row and a job.

Coalescing keeps the *fetches* bounded, which is good. But storage and fsync'd writes are unbounded. At 100 requests a second with 3 KB bodies that is about 26 GB a day, plus the write lock held for every request. The service then fails on disk full or lock contention. The claim "the dedupe key is the only defence" is also false: it is no defence here.

**Recommendation.**
- For sources whose id is not signed, add a unique index on `(source, body_sha256)`. An identical signed body is the same event, and a genuine Razorpay retry resends the same body.
- Reject a delivery whose body hash is known under a different id as a duplicate.
- Add a simple per-source request-rate cap before the database write.

**Effort.** 1 h.

---

#### F13: Medium: Polling by creation time misses captures, disputes and refund changes on older payments

**What the design says.**
- `sources.md:107`: "the sweeper polls `GET /v1/payments?from=&to=…` and `GET /v1/refunds?from=&to=` from the checkpoint minus 10 minutes".
- `edge-cases.md:64` (C8): "If no event arrives, the backfill poll finds it."

**Why it's wrong.** Razorpay's `from`/`to` are "UNIX timestamp, in seconds, from when payments are to be fetched", which is creation time. None of these are found by a window of the last 10 minutes:
- a payment authorised on day 1 and captured on day 3;
- a dispute opened on day 40;
- a refund created on day 2 that becomes `processed` or `failed` on day 5.

If the webhook was disabled (after 24 hours of failures), these changes reach the ledger only through nightly verify, and only within its 30-day window.

**Recommendation.**
- On each sweep, re-fetch every ledger payment still in a non-terminal state (`created`, `authorized`, refund pending) younger than 7 days. This is cheap.
- Poll `GET /v1/disputes` (it exists; razorpay-python ships `dispute_collection.json`).
- State the remaining gap in the README.
- This belongs to the Razorpay slice, not pitch-ready.

**Effort.** 2–3 h at slice 7.

---

### B. Database and queue mechanics

#### F14: High: BEGIN IMMEDIATE through SQLAlchemy async needs a chosen pattern; the pool plus busy_timeout gives 503s

**What the design says.**
- `operations.md:163`: "`BEGIN IMMEDIATE` for every write".
- `grilling-log.md:112` (Q61): "SQLAlchemy Core (not the ORM) writes each query once for both dialects, including `ON CONFLICT` and `FOR UPDATE SKIP LOCKED`".
- `operations.md:162`: `busy_timeout=5000`, "past that, the receiver answers 503".

**Why it's risky.** Measured locally:
1. **The documented recipe serialises reads.** SQLAlchemy's recipe for pysqlite and aiosqlite sets `isolation_level=None` and emits `BEGIN` from a `begin` event. Emitting `BEGIN IMMEDIATE` there makes *every* transaction take the write lock, admin reads included. In `exp2`, a read-only transaction waited **1.92 s** behind a 2 s writer. WAL's main benefit is gone.
2. **Per-transaction `exec_driver_sql("BEGIN IMMEDIATE")` on a default engine works** (`exp4`: the second writer waited 1.35 s, both rows committed). But it bypasses SQLAlchemy's transaction API and relies on Python's legacy transaction control (`sqlite3` `autocommit` is `-1`, legacy, on 3.14). It must be written down, wrapped in one helper and tested.
3. **"Each query once" is false for the queries that matter.** The coalescing upsert needs `sqlalchemy.dialects.sqlite.insert(...).on_conflict_do_update(index_where=…)` on one side and `postgresql.insert` on the other. `exp1` confirmed the SQLite form works. `FOR UPDATE SKIP LOCKED` must be dropped on SQLite.
4. **A pool plus busy_timeout sheds load as 503s.** 1,000 concurrent deliveries with pool 50+50 gave **39 "database is locked"**. With the default pool (5+10) there were 0 errors, because the pool queue did the serialising, but latency reached 5.6 s.

**Recommendation.**
- **One writer connection** owned by a small `Writer` that takes an `asyncio.Lock` and issues `BEGIN IMMEDIATE` explicitly. All writes go through it. `busy_timeout` then only matters across processes, which F11 forbids.
- **A separate read-only engine** (`file:…?mode=ro`, `uri=true`) for admin pages, verify and exports.
- Put the two dialect-specific statements (enqueue, claim) in `queue/sql_sqlite.py` and `queue/sql_postgres.py`, and change Q61's wording.

**Effort.** 2–3 h.

---

#### F15: Medium: `synchronous=FULL` throughput makes the storm test flaky and exceeds Razorpay's timeout; rejections amplify floods

**What the design says.**
- `operations.md:161`: "`synchronous=FULL` | Each commit is on disk before we answer 202".
- `grilling-log.md:94` (Q50): "At tens of events an hour the speed cost doesn't matter."
- `testing.md:81`: "**Duplicate storm:** 1,000 concurrent deliveries of one event | … 999 deliveries logged as duplicates".
- `data-model.md:73`: every rejection is a row, capped at 100,000.

**Why it's risky.** `exp3`: 1,000 concurrent receiver transactions (event insert, job insert, delivery insert, 3 KB body) on this laptop's NVMe drive:

| Mode | Pool | txn/s | p50 | p99 | max | errors |
|---|---|---|---|---|---|---|
| FULL | 5+10 | 160 | 3.0 s | 6.0 s | 6.2 s | 3 |
| FULL | 1+0 | 179 | 2.9 s | 5.5 s | 5.6 s | 0 |
| NORMAL | 5+10 | 352 | 1.0 s | 2.0 s | 2.8 s | 0 |
| NORMAL | 1+0 | 523 | 1.0 s | 1.9 s | 1.9 s | 0 |

- The storm test as specified will flake: some requests get 503, so "999 duplicates logged" fails. Cloud VPS disks usually fsync slower than a laptop NVMe.
- p99 above 5 s exceeds Razorpay's timeout ("fails to respond in 5 seconds … marked timeout"). Razorpay then retries, which adds more load.
- **Rejections amplify floods.** Every unsigned junk request is an fsync'd write competing with real deliveries. A 200 req/s junk flood saturates the writer on its own. The cap limits size, not write rate.

**Recommendation.**
- Keep `FULL`. The durability argument is right, and real volumes are tiny.
- Change the storm assertion to "every request got 2xx or 503; after settling, exactly one `events` row and one sheet row".
- Count rejections **in memory** per reason, write one aggregate row per reason per minute, and keep at most 100 samples.
- Optional, later: group commit (one writer commits every 20 ms and then answers every waiting request). That keeps `FULL` and multiplies throughput.

**Effort.** 1–2 h.

---

#### F16: Low: Postgres async doesn't run under uvicorn on Windows; CI Postgres is Linux-only

**What the design says.**
- `testing.md:95`: "Python 3.14 on Windows and Linux runners".
- `testing.md:92`: "integration tests on Postgres (service container)".
- `build-order.md:72`: psycopg is in the stack.

**Why it's wrong.**
- uvicorn 0.54's `asyncio_loop_factory` returns `asyncio.ProactorEventLoop` on `win32`. psycopg refuses it: `exp3` gave "Psycopg cannot use the 'ProactorEventLoop' to run in async mode".
- The usual fix, `set_event_loop_policy(WindowsSelectorEventLoopPolicy())`, warns on 3.14: "slated for removal in Python 3.16".
- GitHub: "If your workflows use … service containers, then you must use a Linux runner".

**Recommendation.** Declare Postgres Linux-only (Windows development uses SQLite). Run the Postgres CI job on `ubuntu-latest` only.

**Effort.** 0.5 h.

---

#### F17: Low: Smaller queue gaps: instance id, traceability of coalesced events, lease clock

- **Instance id.** `operations.md:136` says startup will "release any leases held by this host's previous run", but `jobs` has no instance column. On Docker the hostname changes on every recreate. Add `claimed_by` (instance id plus start time) to `jobs`.
- **Rule 7 breaks under coalescing.** "Every job, attempt, row change and log line carries the event id that caused it" (`00-overview.md:94`). A coalesced job keeps only the latest event (`data-model.md:111`), and `events` has no job link. The event timeline page (`operations.md:34`) can't find the job for a folded event. Add `events.job_id`, set on insert.
- **Lease clock.** On Postgres, use `now()` from the database in claim, heartbeat and reap, not `:now` from Python. That leaves one clock.
- **Heartbeat.** Heartbeat doesn't check `lease_until >= now`. That is benign, but say so.

**Effort.** 1 h.

---

### C. External API facts and costs

#### F18: Medium: Stripe's read-request allocation is ignored; probes, backfill and "quick" verify cost more than stated

**What the design says.**
- `queue.md:126,128`: a probe every 60 s while a sink or source is paused.
- `sweeper.md:47`: past the horizon, "list every PaymentIntent … and give each a ledger job … a payment we already hold just costs one API call".
- `verify.md:45`: quick mode, "counts and sums per currency | a few calls".

**Why it's wrong.** Stripe: "Your account's read API requests must not exceed an average of 500 per transaction … Every account … has a minimum allocation of 10,000 read requests per month." A small buyer with 20 payments a month gets 10,000.
- The sweeper alone costs 144 × 30 = **4,320** reads a month.
- A source paused for a week costs another **10,080** in probes, which exceeds the allowance.
- Backfill lists PaymentIntents (with the state already in the response) and then fetches each one again, doubling reads.
- Quick verify must list every PaymentIntent in the window to sum it. Stripe has no aggregate endpoint. So "quick" costs the same source calls as full check 1, hourly.

**Recommendation.**
- Put a monthly read budget table in `operations.md`.
- Probes back off: 1, 2, 5, then every 15 minutes.
- Backfill and repair write the **listed** state directly. A listed PaymentIntent with `expand[]=data.latest_charge` is the current state.
- Quick verify = ledger vs sheet only (no source calls). Source totals nightly.

**Effort.** 1–2 h.

---

#### F19: Medium: "≤10 Sheets calls for 200 events" is false once the third call and the fetch rate limit are counted

**What the design says.**
- `edge-cases.md:110` (G1): "At most 10 Sheets API calls: two per **flush** (one read, one `batchUpdate`)."
- `testing.md:80`: "200 rows in the sheet, in no more than 10 Sheets API calls".
- `showcase.md:20`: "The API-call counter stops at about 6".
- `grilling-log.md:121` (Q63) added a third call per flush.

**Why it's wrong.**
- A flush is now three calls (read, write, check).
- The ledger worker fetches through a 10-per-second token bucket (`sources.md:20`). 200 payments take at least 20 s of fetches. More in practice: `payment_intent.created` and `.succeeded` arrive about a second apart, so many payments get two fetches.
- The flusher runs every 3 s, so 200 real payments produce about 7–14 flushes: **21–42 Sheets calls**.
- The claim holds only in CI, where FakeStripe answers instantly.
- In the scene, the demo's own PaymentIntent creation shares Stripe's sandbox limit ("Sandbox: 25 requests per second", and 25 per endpoint).

**Recommendation.**
- Restate as "Sheets calls grow with wall-clock time (3 per 3-second flush while work arrives), not with the number of events; 200 events never cost 200 calls".
- Measure the real number in rehearsal and print that number in the README.
- For the scene, create the 200 payments before starting the flusher, or show "200 rows, N calls" with the real N.

**Effort.** 0.5 h (docs and test).

---

#### F20: Low: Fact-check of external API claims: mostly right, five corrections

| Claim (where) | Verdict | Evidence |
|---|---|---|
| Stripe live retries ~3 days; sandbox "3 times over a few hours" (`sweeper.md:13–14`) | **Correct** | Stripe webhooks doc |
| `construct_event(..., tolerance=300)` checks past only; add a future check (`sources.md:56`) | **Correct** | stripe-python 15.6.1 `_webhook.py` L189: `if tolerance and timestamp < time.time() - tolerance` |
| Multiple `v1` during a roll; old secret for up to 24 h (Q17) | **Correct** | "delay its expiration for up to 24 hours … Stripe generates one signature per secret" |
| `/v1/events` 30 days; ≤20 `types`; 16 handled types exist | **Correct** | events list and types reference; all 16 types listed |
| "auto-paginated oldest first" with `created[gte]` (`sources.md:80`) | **Wrong** | Chronological only with `ending_before` (F03) |
| "only ids are read from the event payload, so the webhook endpoint's own API version doesn't matter" (`sources.md:64`) | **Half right** | True for fetch mode. False for payload mode and for body hashes (F02, F09) |
| `GET /v1/disputes?payment_intent=` (`sources.md:64`) | **Correct** | disputes list has `payment_intent` |
| Dispute status is `open`, `won`, `lost` (`data-model.md:86`) | **Incomplete** | Eight values (F10) |
| A failed refund lowers the refunded amount (`sources.md:48`) | **Plausible, not confirmed** | The refunds doc says the refund goes `failed` and funds return "up to 30 days" later. It doesn't state `amount_refunded`. Record a `refund.failed` fixture in test mode |
| Stripe test mode "about 25 requests a second" (Q36) | **Correct**, plus a per-endpoint 25/s limit and the read allocation (F18) | rate-limits doc |
| Razorpay: `x-razorpay-event-id`, 5 s timeout, 24 h then disabled, emails the alert address | **Correct** | Razorpay best practices |
| Razorpay payments list `from`, `to`, `count`≤100, `skip` | **Correct**, but by creation time (F13) | Fetch All Payments |
| Sheets 60 reads and 60 writes per minute per user; 300 per project | **Correct** | Sheets limits |
| "Google's limit is 2 MB" (`sheets-sink.md:52`) | **Wrong wording** | "no hard size limits … we recommend a 2 MB maximum payload". The real hard limit is "180 seconds" of processing per request |
| `batchUpdate` all or nothing | **Correct** | "All Sheets requests are applied atomically" |
| 10 M cells, 50,000 characters per cell | **Correct** (per the inventory; not re-fetched) | |
| Typed `stringValue` stays literal text | **Consistent with the API** (formulas go in the separate `formulaValue` union member); keep the planned e2e check | ExtendedValue reference |
| Developer metadata as a later fix (`sheets-sink.md:120`) | **Doesn't scale** | 30,000 characters per sheet (F05) |
| Over-quota calls | **New** | "Exceeding the quota request limits is planned to incur charges to your Google Cloud billing account later in 2026." Retrying 429s blindly may one day cost money |

---

### D. Testing

#### F21: High: The fakes encode the same assumptions as the design; the biggest bugs here would pass CI

**What the design says.**
- `testing.md:27`: "A **fake** behaves like the real service, failures included".
- `testing.md:23`: "CI never holds real credentials".
- `testing.md:35`: fixtures are real test-mode events.
- `testing.md:63`: crash points "do nothing unless a test enables them".

**Why it's risky.** FakeStripe and FakeSheets are written by the same person, from the same beliefs:

| Bug | Why the fake agrees with the design |
|---|---|
| F02 | FakeStripe resends identical bytes, and the sweeper's copy is the fixture itself |
| F03 | FakeStripe's `/v1/events` pages "oldest first" because the design says so |
| F05 | FakeSheets sorts only when told, never between read and write of one flush |
| F08 | FakeSheets resolves ranges by id |
| F19 | The fake fetch is instant |

The inventory already recorded this pattern in sync-engine: its in-memory Sheets fake made `appendDimension` a no-op and stored pastes verbatim, "so the tests can't catch coercion" (bug 17).

Recorded fixtures fix *shapes* only, not *behaviour*: ordering, byte-equality, timing, errors.

**Crash points as exceptions prove the wrong thing.** An exception runs `finally` blocks and context managers (rollback, lease release), which a real kill never does. A test that "kills" by raising can pass because the shutdown path cleaned up.

**Recommendation.**
1. **Slice 0 spike (2–3 h).** With the Stripe CLI and a real test sheet, record:
   - a delivery, two resends, and the same event from `/v1/events`, so the bytes can be diffed;
   - a two-page events listing, to see its order;
   - a `refund.failed`;
   - Sheets responses after rename, unshare, sort and a deleted tab.
   The fakes replay these recordings.
2. **A weekly scheduled GitHub Action** with a Stripe **test-mode restricted key** (it can't move money) and a throwaway sheet's service account, stored as repository secrets. It runs the end-to-end scenes headless. This changes Q51.
3. **Crash points kill a subprocess** (`os._exit(137)`), as driftless's `crashpoint` does. Keep in-process fault hooks only for unit tests.

**Effort.** 4–6 h.

---

#### F22: Medium: Invariants and crash points miss the failure modes that matter

**What the design says.** `testing.md:39–44` lists four invariants that must hold "once everything has settled (no job waiting or running, no sink paused)". `testing.md:63–72` lists the crash points.

**Gaps.**
- **Settling excludes paused sinks**, so a sink that pauses wrongly or never resumes passes. Assert: "no sink paused unless the scenario broke it".
- **Missing invariants:**
  - (5) no ledger row ever moves to an older source state (F04);
  - (6) no cell outside our columns or our rows changes (F05–F07);
  - (7) no conflict is recorded without a semantic difference (F02);
  - (8) no job ends `dead` unless the scenario injected a permanent error;
  - (9) at most N alerts per scenario.
- **Invariant 4** ignores `resolved` and the new `superseded` state.
- **Missing crash points:**
  - `after_flush_claim` (claimed, nothing written);
  - `between_sheet_write_and_check`;
  - `after_swap_before_store` (F06);
  - `mid_reap` (the reaper's transaction open);
  - `during_shutdown_release`;
  - `mid_slice` for the new sweep (F03).
- **The race test** asserts "completed exactly once", not "final row equals the last source state" (F04).

**Effort.** 2 h to write down; tests grow with each slice.

---

### E. Security and privacy

#### F23: Medium: Full secret keys allowed; a 403 on a secondary call pauses the whole source

**What the design says.**
- `receiver.md:105`: Stripe `sk_test_` or `sk_live_`, "also `rk_` restricted keys".
- `queue.md:128`: "A revoked source key is a `pause` error for the **source**".
- `sources.md:78`: "401 or 403 → `pause`".

**Why it's risky.**
- A leaked `sk_live_` key on a $5 VPS can refund, pay out and read every customer. The service only needs to read.
- With a restricted key missing "Disputes: read", the first disputed payment gets 403 on `GET /v1/disputes` and pauses the **whole source**. From then on no payment reaches the sheet, over a permission for one column.
- Razorpay API keys are not scopeable to read-only. That needs to be said to the buyer.

**Recommendation.**
- Live mode requires an `rk_live_` key with read on PaymentIntents, Charges, Refunds, Disputes, Events, Balance transactions and Webhook endpoints. `doctor` fails on `sk_live_` unless `--allow-secret-key`.
- `doctor` makes one cheap read per resource and names the missing permission.
- A 403 on a secondary call (disputes, balance transaction) sets that column to "unknown", records a verify finding, and doesn't pause.
- The handover note says Razorpay keys are full-access, so they live only in the server's environment and are rotated at handover.

**Effort.** 1–2 h.

---

#### F24: Medium: Admin login shares the public host, with one token and no throttling

**What the design says.**
- `operations.md:40`: "Every admin page and action needs `ADMIN_TOKEN` (a bearer header, or a login form that sets a secure cookie) … Each token has a label".
- `operations.md:170–176`: Caddy fronts the app on the public HTTPS host.

**Why it's risky.**
- In Compose the app must listen on `0.0.0.0` inside the container, so the admin UI is on the internet at the same hostname as the webhook. The login form has no rate limit or lockout, and token strength is whatever the buyer typed.
- "Each token has a label" contradicts a single `ADMIN_TOKEN` variable.
- What the cookie holds (the raw token, or a signed session) is unspecified.

**Recommendation.**
- Caddy routes only `/webhooks/*` and `/health` publicly. Admin is reached by SSH tunnel or a second, non-published port.
- If admin must be public: require at least 32 random characters (`doctor` checks), throttle login (5 tries per minute per IP), compare with `hmac.compare_digest`, and issue a signed session cookie (`HttpOnly`, `SameSite=Strict`, `Secure` behind TLS).
- Define `ADMIN_TOKENS=label:token,…` if labels stay.

**Effort.** 1–2 h.

---

#### F25: Medium: Personal data in payloads, events and backups; DPDP and GDPR roles unaddressed

**What the design says.**
- `data-model.md:63`: payload kept 90 days; events kept forever.
- `operations.md:152`: `VACUUM INTO backups/…`, keeping 7.
- `grilling-log.md:51` (Q21): masking and column choice.

**Why it's risky.**
- Stripe charge payloads carry billing name, email, phone, address and card brand and last 4. Razorpay payloads carry email and phone (`contact`).
- These sit unencrypted in the live database *and* in 7 backups on the same disk.
- The admin event timeline shows them.
- `rejections.headers` keeps client IPs.
- India's DPDP Rules 2025 were notified 13 November 2025. The substantive obligations (notice, security safeguards, breach notice, processor contracts) start 18 months later, around **13 May 2027**. That is after Lalit's engagements end, but during the client's use. The buyer is the data fiduciary and Lalit, while he operates it, a processor. GDPR Article 28 applies to EU customers of a non-Indian buyer.
- The plan's client-data rule covers AI tools, not operating access.

**Recommendation.**
- Store only the fields the adapters read. Strip `billing_details.address`, `phone`, `payment_method_details` and IP before storing the payload, and keep the hash of the raw body for audit.
- Encrypt backups with a buyer-held key (age or gpg) and copy them off the box (F28).
- A one-paragraph data-processing clause in the quote template.
- Alert bodies carry ids and counts, never emails or descriptions.
- Retention settings stated in the handover.

**Effort.** 2 h plus a template.

---

#### F26: Low: Google service-account keys are blocked by default in newer Google Cloud organisations

**What the design says.** `operations.md:111` and `sheets-sink.md:104`: a service-account JSON key, shared by the buyer.

**Why it's risky.**
- Google: "Google Cloud security baseline constraints are enforced for all organizations created on or after May 3, 2024". The list includes `iam.managed.disableServiceAccountKeyCreation`. A buyer on Google Workspace with a newer organisation cannot create the key without an admin changing policy.
- Workspace admins often block sharing outside the domain, and the service-account address (`…@…iam.gserviceaccount.com`) is outside it.
- Plan says the client owns every account, so this lands on the buyer's admin during handover.

**Recommendation.**
- `doctor` explains both errors in plain words.
- Document the fallback: the key lives in a project under Lalit's or the buyer's personal Google account (no organisation), and only the one spreadsheet is shared with it.
- Longer term, consider an Apps Script in the buyer's sheet that *pulls* rows from `GET /export` with a token. That needs no Google credentials on the server at all.

**Effort.** 1 h (docs).

---

### F. Operations

#### F27: High: No alert when the process or host is down; `/ready` fails on a paused source and invites restarts

**What the design says.**
- `operations.md:76`: "One outgoing URL (`alerts.url`) … plus a red banner".
- `sweeper.md:63`: the silent-webhook alert fires when "no delivery has arrived for 24 hours while sweeps keep finding activity".
- `operations.md:144`: "`GET /ready`: 200 if the database answers, migrations are current, and no source is paused. Docker's health check uses it."

**Why it's wrong.**
- **When the process, container or VM is down, nothing alerts.** Every check runs inside the dead process. This is the most likely failure for a $5 VPS: disk full, an OOM kill, a provider reboot, an expired domain. The buyer notices when the sheet goes quiet. Stripe keeps retrying for 3 days. Razorpay disables the webhook after 24 hours and emails whatever alert address the buyer set.
- **Readiness tied to "no source paused"** means a revoked Stripe key makes the container unhealthy. Compose ignores that, but Fly, Railway, Render, Kubernetes and Swarm restart or de-route unhealthy instances. Then deliveries (which the receiver could still store) get errors, and a data-plane problem becomes an outage.
- Q45 (`grilling-log.md:89`) defines `/ready` differently: "database reachable, source keys work".

**Recommendation.**
- `/ready` = the process can store a delivery (database writable, migrations current). Paused sources and sinks show on `/status`, not in readiness.
- **Dead-man's switch.** The sweeper pings a heartbeat URL (healthchecks.io's free tier, or Uptime Kuma run elsewhere) after each successful sweep. A missed ping alerts by email or Telegram from outside.
- **Uptime check.** An external monitor hits `https://host/health` every 5 minutes.
- Put both in `doctor` and in the handover checklist.

**Effort.** 1 h.

---

#### F28: High: Hosting, cost, TLS, upgrades, off-box backups, support and the April 2027 end are undecided

**What the design says.**
- `grilling-log.md:28` (Q5): "Docker Compose so a client can run it anywhere."
- `operations.md:176`: "`compose.yml` has the app, a Caddy container for HTTPS".
- `operations.md:152`: backups into `backups/` on the same volume.
- `plan.md`: "Two rounds of changes and 14 days of free bug fixes … Upkeep is a separate monthly line and ends in April."

**Why it's risky.**
- **"Anywhere" is not true.** The service needs an always-on process (sweeper, verify and flusher timers) and a persistent disk (SQLite).
  - Scale-to-zero platforms stop the loops, and cold starts can exceed Razorpay's 5 s.
  - Ephemeral-disk platforms lose the ledger on redeploy. Cloud Run and similar serverless hosts are out unless using Postgres and a minimum of one instance.
  - Workable: a small VM, or a PaaS with a volume. The smallest always-on VMs cost roughly $4–12 a month at list price (check at quote time).
- **TLS needs a domain.** Caddy's automatic TLS needs a DNS name pointing at the box. Many small buyers have no one to set that.
- **Upgrades.** "A reusable starter forked per client" (Q7) means every core fix is cherry-picked into N diverging forks.
- **Backups on the same disk** don't survive the disk, the VM or the account.
- **Support.** The likely calls: "sheet stopped updating" (a paused sink after the buyer edited headers), "rows look wrong after I sorted", alert noise, the VM bill. All arrive after the 14 days.
- **After 30 April 2027**, payment infrastructure keeps running with no one who knows it.

**Recommendation.** Decide and write `docs/design/hosting.md`:
- One blessed platform (a PaaS with a volume and a template, or a VM with a one-command installer), with its monthly cost in the quote.
- The client repo **pins a released version** of the core (a tag or an image) instead of forking. Client changes live in config and adapter files.
- Nightly backups copied off the box (rclone to the buyer's Drive or S3; encrypted, F25).
- A handover runbook: the five failure calls above and their fixes.
- An upkeep line ($75–150 a month per `market-analysis.md` §6.3) that ends 30 April 2027, with a written transfer plan: the buyer's next developer gets the runbook and the admin token.

**Effort.** 3–4 h of writing; 2–4 h per client deployment.

---

#### F29: Medium: `doctor` doesn't check the Stripe endpoint's subscribed event types or API version

**What the design says.** `operations.md:62–72` lists the `doctor` checks; `sweeper.md:59–65` describes the silent-webhook alert.

**Why it's wrong.** The buyer configures which events the Stripe endpoint sends. If `refund.updated` or the dispute events are not ticked:
- Every such event arrives only through the sweeper, up to 10 minutes late.
- Every sweep finds "events that never arrived as deliveries", so the silent-webhook alert fires **hourly, forever**, and buyers learn to ignore it.
- The endpoint's API version decides the delivered shape (F02).

**Recommendation.** `doctor` calls `GET /v1/webhook_endpoints` (needs "Webhook endpoints: read"), finds the endpoint whose URL matches `--url`, and fails if any handled type is missing. It warns if the endpoint's version differs from the pinned version. The silent-webhook check ignores types the endpoint isn't subscribed to, and reports them once.

**Effort.** 1 h.

---

### G. Product, scope and schedule

#### F30: Critical: Slices 1–6 are 80–120 h against 4–8 build hours a week; pitching starts 8 October

**What the design says.**
- `build-order.md:36`: "**Pitch-ready = slices 1-6** … the proof layer is not cut to hit a date".
- `plan.md` timeline: "1-7 October: PDF demo, API demo … Pitching starts 8 October."
- The session log (29–30 Sept): demo 1's M1 moved to "mid-to-late November"; demo 3 is also fully designed.

**Why it's wrong.** See the estimate per slice in the Schedule section: 80–120 hours for slices 0–6.
- Lalit's week is 10–15 hours. Five prototype-backed pitches a week take 5–7 of them.
- Demo 1 and demo 3 compete for the rest. That leaves **4–8 build hours a week** for this repo: 11–30 weeks, so pitch-ready between mid-December and April.
- Even at a full 15 hours a week it is 6–8 weeks, late November.
- Three designs and no code on 30 September. The first pitch on 8 October has nothing to show from any of them.

**Recommendation.** Adopt the pitch-ready cut in the Schedule section (24–28 hours, three scenes). Keep the full design as the roadmap. Answer Q65 now (see Questions).

**Effort.** Planning only.

---

#### F31: High: The buyers who post want app-state sync and subscriptions; the Sheets payments log is commoditised

**What the design says.**
- `showcase.md:9`: "The demo earns a $300-500 price only by showing what no-code tools can't".
- `00-overview.md:103–104`: v1 leaves out "a client's own database" and "Subscriptions, invoices, payouts, customers as their own rows".
- `CONTEXT.md:196–198`: the buyer is "a founder or small business taking payments".

**Why it's risky.**
- **What people post.** The vault's only fixed-price Stripe-webhook post is "Fix Stripe Webhook & Supabase DB Sync for SaaS MVP", **$120**: map `invoice.paid` and `customer.subscription.deleted` into Supabase, "done today/tonight" (agent report 07, §A). The baseline's other example: "Fix Stripe webhook issue where user status does not update after payment", **$10**. Both are *app-state* jobs: subscriptions and entitlements into the buyer's own database. v1 does neither.
- **What already exists for a Stripe → Sheets log** (details in the Competitors section):
  - Coupler.io: $24–32 a month, daily refresh.
  - Stripe App Marketplace sync apps: AutoSync, daily.
  - A Workspace add-on with payout reconciliation, "Free of charge with paid features".
  - Pabbly Connect: $14 a month; popular in India for Razorpay → Sheets.
  - Stripe's own Apache-2.0 **sync engine**, which ships a `destination-google-sheets` package.
  - Sigma: from $10–15 a month.
  A buyer who asks "can't I just use X?" has a cheap answer.
- **The proof layer is the moat, but a thin one to sell.** Duplicate rows hurt people using Zapier or n8n. Those people also have Hookdeck (free up to 10,000 events a month, with deduplication) or n8n's "Remove Items Processed in Previous Executions" node. Whether that node handles concurrent runs is disputed in the community (search snippet; not verified).

**Recommendation.** Position in three layers:
1. **For the video and the portfolio**, keep the Sheets mirror. It is visual and the scenes are strong.
2. **For paid work**, pitch "a Stripe/Razorpay webhook that can't double-count, feeding *your* database and your sheet". Make the **Postgres/Supabase business-row sink** and **subscription and invoice events** (`checkout.session.completed`, `invoice.paid`, `customer.subscription.*`) the first adapter after pitch-ready. That is where the posted jobs are, at $120–250. With the starter such a fix takes 2–4 hours, which fits the plan's $100–150 small-job band, and a scoped "sync plus sheet plus proof" job can clear the $250 floor.
3. **For the Sheets buyer**, sell what connectors don't do:
   - updates within seconds (Coupler's hourly refresh starts at $199–259 a month);
   - no monthly fee and no third-party data processor;
   - the nightly proof;
   - custom columns (GST on fees, order ids from metadata, Stripe and Razorpay in one sheet in INR).

Answer "can't I just use Coupler?" in the README.

**Effort.** 1 h of positioning. The sink and subscriptions are 8–15 h after pitch-ready.

---

#### F32: High: The sheet lacks fees, net, product, order id and payment method

**What the design says.**
- `sheets-sink.md:13`: the default columns are Payment ID, Source, Created, Status, Amount, Currency, Refunded, Dispute, Customer email, Description, Written from, Last event, Updated.
- `sources.md:76`: `description` = the PaymentIntent's `description`.

**Why it's wrong.**
- **Fee and net** are the first thing a bookkeeper asks. For Stripe they are in the charge's balance transaction (`expand[]=latest_charge.balance_transaction`, in the same call the design already makes). The same object gives the settlement-currency amount and the exchange rate, which answers "show everything in INR". Razorpay's payment entity carries `fee` and `tax` (GST on the fee), as in razorpay-python's webhook mock.
- **Product and order id.** Checkout Sessions and Payment Links are the most common small-business Stripe setup. There the product and the buyer's order id live on the **Checkout Session**. Stripe's metadata guide shows `metadata[cart_id]` on the session, and it reaches the PaymentIntent only if the integrator passes `payment_intent_data.metadata`. The PaymentIntent's `description` is often empty. So the Description column is blank for exactly the buyers most likely to want a sheet.
- **Payment method** (card, UPI, net banking, wallet) is standard in Indian sales reports.

**Recommendation.** In the pitch-ready cut add:
- Fee, Net, Settlement amount and Settlement currency (1–2 h).
- Method (0.5 h).
- Configurable metadata keys (PaymentIntent `metadata`, Razorpay `notes`) as columns (1 h).
- A "Daily summary" tab: date × currency × gross, refunds, fees, net, count (2 h).

Next: handle `checkout.session.completed` and add Order id and Items (line-item names × quantity) (3–5 h).

**Effort.** About 5 h in the cut; 3–5 h next.

---

#### F33: Medium: Dates written as text break date filters, pivots and monthly totals

**What the design says.**
- `sheets-sink.md:68`: "Times: text in the configured time zone, e.g. `2026-09-30 14:05:12 IST` | Sheets can't re-read it as a date in its own locale".
- `grilling-log.md:50` (Q20).

**Why it's risky.** Text sorts correctly within one zone. But text can't be filtered with "date is between", grouped by month in a pivot table, compared in `QUERY`, or subtracted. Those are the operations a buyer uses for "revenue this month". A typed number with a date format is *not* re-parsed by locale, because the risk Q20 guards against applies only to user-entered strings.

**Recommendation.** Write `Created` and `Updated` as `numberValue` (a serial date in the buyer's zone) with `numberFormat {type: DATE_TIME, pattern: "yyyy-mm-dd hh:mm:ss"}`, and name the zone in the header ("Created (IST)"). For a DST zone, add an optional text column with the abbreviation for the repeated hour.

**Effort.** 1 h.

---

#### F34: Medium: Over-engineering for a solo demo

**What the design includes that a buyer won't see and the demo doesn't need before pitching:**

| Item | Where | Why cut or defer |
|---|---|---|
| Postgres dialect, `--workers N`, the multi-worker race suite | Q4, Q26, Q54, slice 8 | Tens of payments an hour. Doubles the SQL, the CI matrix and the failure modes (F04, F16). Add when a client asks |
| Payload mode | ADR 0002, `ledger-worker.md` | F09 |
| Rebuild by tab swap, cell-limit tracking, the deployment-id check | `sheets-sink.md` | Repair in place covers the real cases (F06); a cell alert at 80 % is enough |
| Per-sink pause with 5-failure counting and a probe | Q30 | Pitch-ready can stop and alert on 401/403/404, with `wsync resume` |
| `rejections` table | `data-model.md:73` | Counters (F15) |
| Seven admin pages with htmx live counters | Q58 | The Sync status tab, the CLI and logs cover the video; one read-only page is enough for the pitch |
| Eleven alert kinds, hourly digests | `operations.md:80–90` | Three alert kinds cover the pitch: dead jobs, paused, verify differences |
| Hypothesis property suites with crash-restart | Q52 | Keep five scripted scenarios with subprocess kills; add Hypothesis after pitch-ready |
| DST handling for nightly verify | H11 | Buyers in India have no DST; defer until a US or EU buyer |
| `secret_index`, conflict tracking, multiple token labels | various | Small but each costs tests |
| Windows + Linux CI on every push | `testing.md:95` | Windows runners consume private-repo minutes faster (GitHub bills Windows at a multiplier; not re-checked today). Run Windows weekly |

**Effort.** The cut saves roughly 50–70 hours before pitch-ready.

---

#### F35: Medium: Contradictions between pages, ADRs and the grilling log

| # | Contradiction | Where |
|---|---|---|
| 1 | `payment_intent.created` is **handled** in `sources.md:62`, but A10 gives it as an example of an **unhandled** type ("`payment_intent.created`, `customer.updated`") | `edge-cases.md:39` |
| 2 | Q12 lists four PaymentIntent types; `sources.md` lists seven | `grilling-log.md:42`, `sources.md:62` |
| 3 | Payload guard: "unless newer" vs "and newer"; ADR says a revoked key triggers payload mode, the queue page says pause | F09 |
| 4 | G1 and Q53: "two per flush", ≤10 calls. Q63: three per flush. Showcase: "about 6" | F19 |
| 5 | `/ready`: Q45 "source keys work" vs `operations.md:144` "no source is paused" | F27 |
| 6 | Ledger workers: Q26 "one ledger worker" vs `operations.md:13` "ledger worker (one per source)" vs `queue.md:145` "one worker" | |
| 7 | Q35 Sync status "at no extra API cost" vs `sheets-sink.md:21` "at least every 10 minutes when idle" | |
| 8 | ADR 0002 flag `from_payload` vs `data-model.md:89` `written_from = 'payload'` | |
| 9 | `sweeper.md:43`: origin "which the sheet and the video show". The default columns have no origin column (`sheets-sink.md:13`) | F36 |
| 10 | Scene 3: "backoff visible … then … paused: 403". A 403 is a pause-class error: paused at once, no backoff | `showcase.md:19`, `queue.md:101` |
| 11 | Overview "Q1–Q61" and "design complete" vs the grilling log's Q65 (open) and "The design is being worked out" | `00-overview.md:7,126`, `grilling-log.md:7,123` |
| 12 | "Each token has a label" vs a single `ADMIN_TOKEN` | `operations.md:40`, F24 |
| 13 | A3 lists five security headers; `receiver.md:50` lists six (adds `x-razorpay-event-id`) | `edge-cases.md:32` |
| 14 | The overview's part table says the ledger worker upserts "with **terminal states** sticky", which the refined rule dropped for fetch mode | `00-overview.md:79` vs `sources.md:50` |
| 15 | Q23 "always written RAW" vs typed `updateCells` values (same intent, different mechanism) | `grilling-log.md:53`, `sheets-sink.md:63` |

**Effort.** 1 h.

---

#### F36: Medium: Three of the five scenes can't be shot as written

| Scene | Problem | Fix |
|---|---|---|
| 1 Duplicate storm | Depends on resends being byte-identical, or they count as conflicts with an alert (F02) | Semantic conflict; rehearse in slice 0 |
| 2 Outage, "each row tagged `sweep`" | The sheet has no origin column | Add an optional "Via" column (delivery/sweep), on for the demo, or show the admin Events page |
| 3 Access revoked, "backoff visible … then paused" | 403 pauses at once | Script it as "paused: 403 within seconds; jobs waiting 10 → 10; re-share; Resume; drained" |
| 4 Burst, "counter stops at about 6" | ~21–42 calls with real Stripe (F19); the sandbox's 25 req/s limit is shared with the script's own creates | Pre-create the payments, show the real count |
| 5 End card | Fine | — |

Also: `wsync demo reset` "Empties … the test spreadsheet's Payments tab". Make sure `demo` refuses unless the spreadsheet id is on a configured demo allow-list, not only "every source is in test mode". A mistyped id would empty a real sheet.

**Effort.** 1 h plus rehearsal.

---

#### F37: Low: Glossary slips and stale status lines

- "The client pins its API version" (`sources.md:64`, `edge-cases.md:104`): *client* is reserved for freelance work. Say "our Stripe API client" or "the adapter".
- "a sync in progress" (`edge-cases.md:137`): *sync* is avoided. Say "a flush in progress".
- "one true record" (ADR 0001:5): *record* is avoided for business rows.
- "the client's provider" (`operations.md:152`): two avoided words in three.
- `operations.md` uses "client" for the buyer throughout ("how a client runs it"). Decide: in handover text, "client" is the freelance client, who *is* the buyer. Add that note to `CONTEXT.md`.
- Status lines: see F35 #11.

**Effort.** 0.5 h.

---

#### F38: Low: "Every night it proves your sheet matches Stripe" is wider than what verify checks

**What the design says.**
- `00-overview.md:11`: "Every night it proves that the sheet matches the source."
- `verify.md:20`: source vs ledger for "the last 30 days", by creation date.
- `sources.md:82`: the source list is `/v1/payment_intents`.

**Why it's an overclaim.**
- A refund or dispute on a payment created 31+ days ago is never compared with the source.
- Charges without a PaymentIntent are ignored (`no_payment_intent`) and invisible to verify, so a buyer on an old integration sees "0 differences" and an empty sheet.
- Row checks compare the ledger with the source *through the same mapping*, so mapping bugs (F10) are invisible.

**Recommendation.**
- Word it as "every night it proves the last 30 days of payments match Stripe".
- Add one independent total: the sum of balance transactions of type `charge` and `refund` for the window vs the ledger's net. It uses a different object and catches mapping errors and objects outside the PaymentIntent model.
- Report the ignored-event count on the verify line.

**Effort.** 1–2 h.

---

## Missing features customers need

| Feature | Who asks | How often | Effort | Belongs in | Why |
|---|---|---|---|---|---|
| Fee, net, settlement amount and currency | Bookkeepers, founders, anyone with non-INR/USD sales | Almost every buyer | 1–2 h (one `expand`; Razorpay `fee`, `tax`) | **Pitch-ready** | First question on any payments sheet; nearly free |
| Payment method (card brand, UPI, net banking, wallet) | Indian D2C, support teams | Often | 0.5 h | **Pitch-ready** | Standard column in Indian sales reports |
| Metadata or notes keys as columns (order id, SKU, user id) | Anyone with an order system | Very often | 1 h | **Pitch-ready** | Joins the sheet to their system |
| Checkout Session order id and line items (product × qty) | Payment Links and Checkout users, course sellers | Very often | 3–5 h | **v1, right after pitch-ready** | Without it the Description column is blank for the most common setup |
| Daily or monthly summary tab (gross, refunds, fees, net, count per currency) | Founders, bookkeepers | Very often | 2 h | **Pitch-ready** | Stops wrong `=SUM()` over pending rows (F10); looks good on video |
| Per-payment notification (Slack, Discord, Telegram "New sale ₹999") | Small shops, founders | Very often | 1–2 h (reuse the alert sender as a sink) | **Pitch-ready (optional)** | Cheap and visible; the thing people build Zaps for |
| Postgres or Supabase business-row sink (their app database) | SaaS founders | Often, and the paying posts | 4–8 h | **v1 (first paid adapter)** | Where the $120–250 webhook jobs are (F31) |
| Subscriptions and invoices (`invoice.paid`, `customer.subscription.*`, entitlement status) | SaaS founders | Very often in posts | 8–15 h | **v1 (with the database sink)** | The most common posted Stripe-webhook job |
| Forward deduplicated events onward (signed POST to n8n, Zapier, Make) | No-code users hurt by duplicates | Often | 3–5 h | **Later (early)** | "Keep your n8n flow; put this in front": the exact audience of the pitch |
| Refunds as their own rows or tab (with refund dates) | Accountants (period accounting) | Often | 3–4 h | **Later** | Row-per-payment hides *when* money went back; month-end totals differ |
| Payouts and settlements with bank reconciliation (Stripe payout id and arrival date; Razorpay `settlement_id`, UTR, settlement recon) | Bookkeepers, CAs | Often | 6–10 h | **Later (first add-on for bookkeepers)** | What connects the sheet to the bank statement (and to demo 1) |
| GST: Razorpay fee GST (`tax`); sales tax from Stripe Tax or invoices | Indian CAs, businesses | Often in India | 0.5 h (fee GST); 4+ h (sales tax) | Fee GST **pitch-ready**; sales tax **later** | GST on fees is an input credit CAs claim |
| Dispute deadline (`evidence_details.due_by`, Razorpay `respond_by`) and reason | Anyone with disputes | Sometimes | 0.5 h | **v1** | A missed deadline loses the dispute |
| Failure reason (`last_payment_error.code`, Razorpay `error_description`) | SaaS dunning, support | Sometimes | 0.5 h | **v1** | Explains failed rows |
| Customer phone (Razorpay `contact`), masked, opt-in | Indian D2C (WhatsApp follow-up) | Often in India | 0.5 h | **v1 (off by default)** | Common ask; privacy default stays off |
| Customers tab | SaaS, CRM users | Sometimes | 3 h | **Later** | |
| Multi-account (two Stripe accounts, or Stripe + Razorpay in one sheet with a reporting currency) | Agencies, multi-brand D2C | Sometimes | 4–6 h | **Later, but make the key `(source, account, id)` now** | Changing the key later means a migration of every table |
| CSV or Excel export with formula escaping | Bookkeepers | Sometimes | 1–2 h | **Later** | Closes the CSV-injection note in the README |
| Tally, Zoho Books or QuickBooks export | Indian CAs, US bookkeepers | Sometimes | 4+ h | **Later** | Pairs with demo 1's Tally sheet |
| Other sinks: Airtable, Notion, HubSpot | Ops teams | Occasionally | 3–5 h each | **Later** | Per client, as an adapter |
| One-click deploy (a PaaS template) plus setup wizard in `doctor` | Non-technical buyers | Every deployment | 4–6 h | **Pitch-ready (template), v1 (wizard)** | "Where does this run?" is a first-call question (F28) |
| Public read-only demo sheet fed by test mode | Upwork and Reddit prospects | Every pitch | 2–3 h | **Pitch-ready** | A link beats a GIF; test data only |
| Stripe Connect or marketplaces | Platforms | Rare for this buyer | Large | **Never (v1)** | ADR 0003 is right |
| Shopify or WooCommerce orders | E-commerce | Often, but a different product | Large | **Never (this repo)** | Their own reports and apps already cover it |

**Notes on the most important ones.**
- **Fee and net plus summary tab** turn a "payments log" into a "sales ledger". The first is sold for $0–35 a month; the second is what a bookkeeper or founder reads. Both are about 4 hours together, and the summary tab is a strong closing shot for the video.
- **Database sink plus subscriptions** is where the posted money is (F31). Do it immediately after pitch-ready, as an adapter, so the core stays small.
- **Forwarding onward** reframes the pitch for n8n users: "don't rebuild your automation; put a deduplicating, catch-up front door in front of it". It uses parts that already exist (a sink that POSTs).
- **Payouts and bank reconciliation** is the bridge to demo 1 ("statement lines matched to Stripe/Razorpay settlements"). It is also the one combined offer that clears $500 comfortably.

---

## Competitors and alternatives

| What | Price (as of 2026-09-30) | What it already does | What it means for the pitch |
|---|---|---|---|
| Stripe Dashboard CSV export and Reports | Free | Manual export of payments, payouts, balance and payout reconciliation | Buyers with a monthly routine don't need a sync. Sell to those who want it live or custom |
| Stripe Sigma | From $10 a month (annual) or $15 (monthly) up to 250 charges; $60 up to 2,500 (Stripe support page) | SQL over all Stripe data, scheduled queries | Cheaper for analysis; not a live sheet; not Razorpay |
| Stripe Data Pipeline | From $50–65 a month (Stripe support page) | Full export to warehouses and cloud storage | For companies with a warehouse, not our buyer |
| Stripe App Marketplace sheet sync apps (AutoSync for Google Sheets; Bearsync seen in a tutorial) | Trial, then paid (pricing on vendor site; not fetched) | Daily sync of payments, subscriptions and customers | Daily, not seconds; no proof; covers subscriptions, which v1 doesn't |
| "Stripe Exporter" Workspace add-on | "Free of charge with paid features" (listing) | Charges, payouts, balance transactions, invoices, customers, subscriptions; payout reconciliation with gross, fee and net that "confirms the total ties to the payout" | Already has the fee, net and payout reconciliation v1 lacks (F32) |
| Coupler.io | Free (manual, 100 rows per run); $24–32 a month daily; $199–259 a month hourly; 15-minute only on custom plans | Stripe → Sheets importer | "Seconds, not hours, and no subscription" is a real edge over the hourly tier |
| Coefficient | Pro "$99/user/month" with hourly refresh (from Coefficient's own comparison page) | Live connectors into Sheets | Same point as Coupler |
| Zapier, Make | Zapier Pro from $19.99 a month for 750 tasks (baseline research) | Stripe trigger → "create or update row" | The race and duplicate problem the design documents; each step is a billed task |
| n8n | Self-hosted free; cloud paid | "Remove Items Processed in Previous Executions" dedupes by key across runs | Partly closes the gap; concurrency and downtime gaps remain (baseline research) |
| Pabbly Connect | Free (100 tasks); $14 a month (10,000 tasks); lifetime from $349; triggers free | Razorpay → Sheets templates widely used in India | Indian buyers already pay very little for "Razorpay to sheet"; sell the proof and GST columns |
| Hookdeck Event Gateway | Free: 10,000 events, 3-day retention; Team from $39 a month | Hosted ingest, exact and field-based dedupe, retries, bulk retry, alerts, forward anywhere | Covers receiving-side reliability for anyone who can wire a destination. Pitch the ledger, sheet and proof, not the receiver |
| stripe/sync-engine (Apache-2.0, TypeScript) | Free | Stripe → Postgres, SQLite, **Google Sheets**; webhooks plus backfill | The same idea from Stripe, free. README: "lacks tight access controls and should only be deployed internally"; development moved to a fork. Our edge: Python, Razorpay, verify, operability |
| Airbyte, Estuary and similar ELT tools | Self-hosted free or usage-based cloud | Stripe source → warehouse; some to Sheets | Heavy for one sheet |
| Accounting integrations (QuickBooks and Xero Stripe feeds, Synder, A2X) | Paid (not priced here) | Fees, payouts, bank reconciliation into the books | What a bookkeeper uses once the business is past the sheet stage. Sell the sheet to earlier businesses |
| Razorpay Dashboard reports and settlement recon | Free | Payments, refunds, settlements, per-payment fee and tax in the recon report | Same as Stripe's exports: a live, custom, proved sheet is the difference |

**What this means.** "Stripe or Razorpay into a Google Sheet" is solved for $0–35 a month at daily refresh. The design's defensible differences are:
1. updates in seconds, not hours, without a $199+ tier;
2. self-hosted, owned by the buyer, no third-party data processor, no subscription;
3. a nightly proof and visible failure handling;
4. custom columns and both Indian and global sources in one ledger;
5. the same core feeding the buyer's own database.

Point 5 is where the posted money is. Lead the README with points 1–3 and the first-call answer "why not Coupler or Zapier".

---

## Schedule reality check

Hour estimates assume Claude-assisted coding by someone who has not built this stack before, and include tests and the first run against real services. They exclude pitching.

| Slice | Scope as designed | Estimate |
|---|---|---|
| 0 | Stripe test account and CLI; Google Cloud project, service account, key (org policy, F26); test sheet; plus the recording spike (F21) | 3–5 h |
| 1 | Config and fail-closed start; migrations in two dialects; receiver with every check; Stripe adapter (verify, describe, fetch, mapping); ledger worker; FakeStripe; invariants 1 and 3 | 14–20 h |
| 2 | Claim, heartbeat, fencing, reaper, three error classes, backoff profiles, dead letters, source pause, graceful shutdown, crash points in subprocesses, property tests | 14–20 h |
| 3 | Flusher, encoding and currency table, Sync status, header check, sink pause and probe, rebuild, cell tracking, deployment id, FakeSheets with quotas, sort and lost replies | 16–24 h |
| 4 | Sweeper with checkpoints and horizon, backfill, replay, silent webhooks, verify quick and full, repair, DST | 12–18 h |
| 5 | Seven admin pages, auth, CSRF and login, eleven alerts, admin actions, `doctor` (nine checks), pruner, backup | 14–20 h |
| 6 | `wsync demo` (five scenes) against real Stripe and Sheets, rehearsals, the first video (5–6 h per the vault's video research), README with GIFs | 12–16 h |
| **Total 0–6** | | **85–123 h** |

**Capacity.**
- 10–15 hours a week, minus 5–7 for five prototype-backed pitches (`plan.md`), minus demo 1 (M1 in November) and demo 3: about **4–8 build hours a week** here.
- That is **11–30 weeks**: pitch-ready between mid-December and April.
- Even with all 15 hours on this repo: 6–8 weeks, around late November.

**Proposed pitch-ready cut (24–28 h; at 8 build hours a week, ready around the end of October):**

| Step | Contents | Estimate |
|---|---|---|
| P0 | Slice 0 plus the recording spike: resend bytes, events order, a real sheet's errors | 2 h |
| P1 | Stripe receiver (size, signature with future bound, mode, id), event plus job in one transaction; queue with **one live job per key and a rerun flag** (F01), lease, token, fenced complete, backoff, dead after N, reaper; one SQLite writer (F14) | 5–6 h |
| P2 | Fetch with `expand[]=latest_charge.balance_transaction`; mapping including fee, net, method, metadata keys; ledger upsert with row hash | 2–3 h |
| P3 | Flusher: read by sheet id (F08), one `batchUpdate` by key, typed values including dates (F33), append after the last used row (F07), never write keys on update (F05); Sync status line; on 401/403/404 stop and alert, `wsync resume`; Daily summary tab | 4–5 h |
| P4 | Sweeper by time slices (F03), at startup and every 10 minutes; `wsync verify` (counts, sums per currency, row compare) writing one line to Sync status | 3 h |
| P5 | Tests: FakeStripe replaying recordings; duplicate storm with the new assertion; burst; three subprocess crash tests (after event commit, after sheet write, mid-slice) | 3 h |
| P6 | Three scenes (duplicate storm, outage catch-up, verify with summary tab), README with honest limits and the "why not Coupler" answer | 5–6 h |
| **Total** | | **24–28 h** (the video is the largest single item; the vault puts a first video at 5–6 h) |

**Deferred to after pitching starts:** Postgres, `--workers`, Razorpay, admin pages (one read-only page at most), rebuild, payload mode, probes and automatic resume, conflicts, the rejections table, the deployment-id check, cell tracking, DST, Hypothesis suites, most `doctor` checks, pruner and backup (document the manual commands).

**Before that (for pitches from 8 October):** an 8–10 hour **pitch prototype** is P1 + P2 + P3 without tests. It records a 20-second GIF of scene 1 (five resends → one row) and scene 2 (stop, pay, start → rows appear). That is enough to attach to the first Stripe-webhook replies.

---

## Questions for the next grilling session

1. **How does the queue keep one job per payment?** (a) One live job per `(stage, sink, payment)` with a `rerun` flag; complete re-queues when set (driftless). (b) Keep waiting-plus-running and supersede on fail, reap and release. **Recommend (a):** it fixes F01 and F04 with less code.
2. **What is a conflict?** (a) A different raw-body hash (as written). (b) A different semantic fingerprint `(type, object id, created, livemode)`, with bytes stored for information. (c) Drop conflicts. **Recommend (b)**, and never compare in the sweeper (F02).
3. **How does the sweep checkpoint?** (a) Per page, newest created seen (as written). (b) Time slices walked fully, checkpoint = slice end from Stripe's clock, 10-minute overlap. (c) An event-id cursor with `ending_before`. **Recommend (b)** (F03).
4. **Who is the paying buyer?** (a) A small business wanting a live sales sheet. (b) A SaaS founder needing Stripe → app database with subscriptions. (c) n8n and Zapier users wanting a deduplicating front door. **Recommend** video and portfolio for (a) and (c), and the first paid adapter for (b) (F31).
5. **What is pitch-ready?** (a) Slices 1–6 as written (85–123 h). (b) The 24–28 h cut above. (c) Only the 8–10 h prototype. **Recommend (b)**, with (c) first for the 8 October pitches (F30).
6. **Q65: which demo is built first?** (a) Demo 1 (statement extractor, M1 November). (b) This repo's cut. (c) Alternate weekly. **Recommend (b) to the pitch prototype, then demo 1's MVP,** because the webhook prototype is smaller and its GIF is ready sooner. But demo 1 serves the stronger market (`market-analysis.md` §4.2 ranks PDF above API glue). Decide by which posts arrive first.
7. **Which columns ship in the cut?** (a) The current 13. (b) Plus fee, net, settlement amount and currency, method, metadata keys, and a Daily summary tab. (c) (b) plus Checkout items. **Recommend (b), with (c) next** (F32).
8. **Who owns the Payments tab?** (a) The buyer edits freely: warning-only protection, tamper handling, rebuild by swap (as written). (b) Machine-owned: keys never rewritten, repair in place, buyers sort in filter views or a View tab. **Recommend (b)** (F05–F07).
9. **Keep payload mode?** (a) Keep. (b) Cut; a 404 flags the row "not found at source". **Recommend (b)** (F09).
10. **Postgres and `--workers` in v1?** (a) Keep in slice 8. (b) Defer until a client asks; Linux-only when added. **Recommend (b)** (F16, F34).
11. **How is a client hosted?** (a) The buyer's VM with Compose and Caddy. (b) A PaaS with a volume and a deploy template. (c) Lalit hosts for a monthly fee until April 2027. **Recommend (b), with (a) documented;** state the monthly cost in every quote (F28).
12. **What happens after 30 April 2027?** (a) Nothing written. (b) A handover runbook plus version pinning, so any developer can take over; upkeep ends on a fixed date. **Recommend (b)** (F28).
13. **External monitoring?** (a) In-process alerts only. (b) Add a dead-man's switch (sweeper pings) and an uptime check on `/health`; `/ready` ignores paused sources. **Recommend (b)** (F27).
14. **May CI hold a Stripe test-mode key and a test sheet?** (a) No real credentials ever (Q51). (b) Yes, in a weekly scheduled workflow, test mode only, restricted key. **Recommend (b)** (F21).
15. **SQLite write path?** (a) Pool plus `busy_timeout` plus a global BEGIN IMMEDIATE hook. (b) One writer connection behind an asyncio lock, explicit BEGIN IMMEDIATE, and a read-only engine; `synchronous=FULL` kept. **Recommend (b)** (F14, F15).
16. **How are rejections stored?** (a) A row each, capped at 100,000. (b) In-memory counters, one aggregate row per reason per minute, 100 samples. **Recommend (b)** (F15).
17. **Stripe key policy in live mode?** (a) Any key. (b) A restricted read-only key required; `doctor` checks each permission; 403s on secondary calls degrade a column. **Recommend (b)** (F23).
18. **Razorpay dedupe?** (a) Event-id header only. (b) Also a unique body hash for sources whose id is outside the signature. **Recommend (b)** (F12).
19. **How are dates written?** (a) Text with the zone abbreviation. (b) Typed date serials with a date-time format and the zone in the header. **Recommend (b)** (F33).
20. **Where is the admin UI reachable?** (a) The public host, token login. (b) Localhost or tunnel only; if public, a long token, throttled login and a signed session. **Recommend (b)** (F24).
21. **What does quick verify do?** (a) Source totals hourly (as written; the same cost as a full listing). (b) Ledger vs sheet only, hourly; source totals nightly. **Recommend (b)** (F18).
22. **What is the business-row key?** (a) `(source, payment_id)`. (b) `(source, account, payment_id)` now, with one account in v1. **Recommend (b):** it is free now and a migration later (missing features, multi-account).

---

## Sources

**Verified locally (2026-09-30)**
- `audit-lab/exp1_queue.py`: SQLite partial unique index, the `ON CONFLICT … WHERE status = 'waiting'` upsert through SQLAlchemy 2.1.1 (`sqlite.insert().on_conflict_do_update(index_where=…)`), and fail/reap collisions (IntegrityError; reaper blocked).
- `audit-lab/exp2_immediate.py`: the global `begin`-hook BEGIN IMMEDIATE recipe; a read waited 1.92 s; the 1,000-delivery storm with pools 5+10 and 50+50 (39 "database is locked").
- `audit-lab/exp3_variants.py`: `synchronous=FULL` vs `NORMAL`, pool vs single connection (throughput and latency table in F15); psycopg async under ProactorEventLoop; the Python 3.14 deprecation warnings for `set_event_loop_policy` and `WindowsSelectorEventLoopPolicy`.
- `audit-lab/exp4_manual_begin.py`: per-transaction `exec_driver_sql("BEGIN IMMEDIATE")` on a default engine; `sqlite3` `autocommit` = −1 (legacy) on 3.14.7.
- uvicorn 0.54.0 `uvicorn/loops/asyncio.py` (`ProactorEventLoop` on win32); stripe-python 15.6.1 `_webhook.py` (tolerance check L189, API version `2026-08-26.dahlia`).
- `D:\second-brain\repo-study\webhook\sync-engine\README.md` and `packages/` (`destination-google-sheets`; access-control warning; the fork note).
- `D:\second-brain\repo-study\webhook\razorpay-python\tests\mocks\fake_payment_authorized_webhook.json` (`fee`, `service_tax`, `notes`, `order_id`, `method`, `contact`), `dispute.json`, `documents/settlement.md` (settlement recon with `fee`, `tax`, `settlement_id`).

**Stripe**
- Webhooks (retries, ordering, duplicates, destination API version, secret roll up to 24 h, new signature per retry): https://docs.stripe.com/webhooks
- Process undelivered events (`ending_before` gives chronological order; 30 days): https://docs.stripe.com/webhooks/process-undelivered-events
- List events, Event object (`types` ≤ 20; rendering by creation-time API version; `pending_webhooks`): https://docs.stripe.com/api/events/list
- Event types: https://docs.stripe.com/api/events/types
- Rate limits and read-request allocations: https://docs.stripe.com/rate-limits
- Refunds (failed refunds, return of funds up to 30 days): https://docs.stripe.com/refunds
- List disputes (`payment_intent` filter): https://docs.stripe.com/api/disputes/list
- Dispute object (eight statuses): https://docs.stripe.com/api/disputes/object
- Metadata (Checkout Session metadata; indirect mappings): https://docs.stripe.com/metadata
- Sigma and Data Pipeline pricing: https://support.stripe.com/questions/understanding-stripe-sigma-and-data-pipeline-pricing
- AutoSync for Google Sheets (Stripe App Marketplace): https://marketplace.stripe.com/apps/autosync-for-google-sheets

**Razorpay**
- Webhook best practices (`x-razorpay-event-id`, 5 s timeout, 24 h retries then disabled, alert email, ordering): https://razorpay.com/docs/webhooks/best-practices/
- Fetch all payments (`from`, `to`, `count` ≤ 100, `skip`): https://razorpay.com/docs/api/payments/fetch-all-payments/
- Settlements entity (`settlement_id`, `utr`): https://razorpay.com/docs/api/settlements/

**Google**
- Sheets usage limits (60 per user per minute, 300 per project; 2 MB recommendation; 180 s; atomic batches; planned over-quota billing): https://developers.google.com/workspace/sheets/api/limits
- Developer metadata limits (30,000 characters per spreadsheet and per sheet): https://developers.google.com/workspace/sheets/api/guides/metadata
- `spreadsheets.get` (ranges in A1 notation): https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/get
- `spreadsheets.getByDataFilter` (GridRange by sheetId; reference, not fetched today): https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/getByDataFilter
- ExtendedValue (`stringValue` vs `formulaValue`): https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/other#ExtendedValue
- Google Cloud security baseline constraints (service-account key creation disabled for organisations created on or after 3 May 2024): https://docs.cloud.google.com/resource-manager/docs/secure-by-default-organizations
- Stripe Exporter Workspace add-on listing: https://workspace.google.com/marketplace/app/stripe_exporter/787255654090

**GitHub**
- Service containers need a Linux runner: https://docs.github.com/actions/tutorials/communicating-with-docker-service-containers

**Competitors (vendor pages)**
- Coupler.io pricing: https://www.coupler.io/pricing
- Coefficient Pro price (Coefficient's own comparison page, a vendor claim): https://coefficient.io/coupler-io-pricing
- Hookdeck pricing and plan summary: https://hookdeck.com/pricing and https://hookdeck.com/webhooks/platforms/hookdeck-event-gateway-vs-convoy-webhook-receiving-comparison
- Pabbly Connect pricing: https://www.pabbly.com/connect/pricing/ and Razorpay → Sheets guide: https://www.pabbly.com/automatically-add-razorpay-payment-details-to-google-sheets/
- n8n Remove Duplicates across executions (community thread; the concurrency caveat is from a search snippet, not verified): https://community.n8n.io/t/how-can-i-prevent-duplicate-workflow-executions-when-a-webhook-is-triggered-multiple-times/303390 and https://www.linkedin.com/posts/zacharia-kimotho_the-real-power-of-the-remove-duplicate-node-activity-7342071530626953216-KWJY
- stripe/sync-engine: https://github.com/stripe/sync-engine (read from the local clone at commit 0cb8ca4)

**Law**
- DPDP Rules 2025 notified; commencement schedule (a law-firm summary): https://www.ilflaw.com/publications/digital-personal-data-protection-rules-2025-notified/ and the Rules text: https://www.dpdpa.com/DPDP_Rules_2025_English_only.pdf
- 18-month date of 13 May 2027 (a vendor guide citing Gazette G.S.R. 843(E)): https://www.privybyidfy.com/blog/dpdp-compliance-guide-2026-what-indian-enterprises-must-do-before-may-2027

**Internal**
- `D:\freelance\webhook-sync\CONTEXT.md`, `docs/design/*`, `docs/research/feature-inventory.md`
- `D:\second-brain\freelance-vault\research\demo-baselines\00-summary.md`, `02-webhook-sync.md`
- `D:\second-brain\freelance-vault\domain\plan.md`, `domain\market-analysis.md` (§1–7)
- `D:\second-brain\freelance-vault\research\agent-reports\07-sub-api-glue-pdf-spreadsheets.md` (§A, §C), `01-categories-market-map.md` (§4, §6)
- `D:\second-brain\freelance-vault\session-log.md` (29–30 September entries)
