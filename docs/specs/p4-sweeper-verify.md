Status: implemented

# P4: catch what webhooks missed, and prove the sheet is right

Feature issue: #37

## Plain-language summary

Webhooks can be missed: the server was down, a deploy went wrong, or Razorpay gave up and disabled the webhook. P4 adds the sweeper, which asks Razorpay every ten minutes for payments and refunds created since it last looked and stores anything missing, plus an hourly re-list that catches later changes such as a capture, a fee or a refund reaching processed. After each healthy sweep it pings an outside heartbeat service, so a dead server is noticed. P4 also adds verify: every hour it checks the sheet against the ledger, and every night it checks the ledger against Razorpay for the last 30 days, repairs what it can through the ordinary jobs, writes one "Verified …" line on Sync status, and alerts on anything it couldn't fix. `wsync sweep`, `wsync backfill` and `wsync verify` run these by hand.

**Rests on:** [build-order.md](../design/build-order.md) step P4; [sweeper.md](../design/sweeper.md), [verify.md](../design/verify.md), [sources.md](../design/sources.md) ("Sweep and polling"), [data-model.md](../design/data-model.md) (`checkpoints`, `verify_runs`, `verify_findings`), [operations.md](../design/operations.md) (commands, alerts, heartbeat), [ledger-worker.md](../design/ledger-worker.md), [showcase.md](../design/showcase.md) (outage scene); [ADR 0002](../design/decisions/0002-fetch-current-state-not-event-payload.md), [ADR 0007](../design/decisions/0007-one-server-process-per-database.md); edge-cases H1-H18, C11, C13; grilling log Q38, Q40, Q41, Q64, Q72, Q86, Q89, Q96, Q107, Q108 and round 15, [Q144-Q157](../design/grilling-log.md); facts in [razorpay-list-facts.md](../research/razorpay-list-facts.md) (G71-G73), [razorpay-first.md](../research/razorpay-first.md) Q3 and Q7 (G50, G53, G56); the code P1-P3 built (each spec's "Decisions made during the build").

## Problem Statement

Everything so far depends on Razorpay delivering every webhook. It doesn't always: it retries a failing delivery for 24 hours and then disables the webhook for good, and it never sends an event for a change it doesn't announce (a fee set at capture, an auto-refund). A payment missed this way never reaches the ledger or the sheet, and nobody can tell. There is also no proof that the sheet still matches the ledger, or the ledger Razorpay, after people edit the sheet or something goes wrong in between, and no outside signal when the server itself has died.

## Solution

The sweeper walks Razorpay's payments and refunds lists in creation-time slices on Razorpay's own clock, with a 10-minute overlap, moving a per-list checkpoint only after a whole slice is stored, and stores each listed item as a synthetic event with a ledger job that fetches. It runs at startup and every 10 minutes; on the hour it re-lists the last 7 days of payments and 21 days of refunds and stores any item whose state changed. A healthy pass pings the heartbeat URL; events found only by the sweeper raise a silent-webhook alert. Verify compares the sheet against the ledger hourly, and Razorpay against the ledger nightly, records each run and finding, repairs through mirror and ledger jobs, writes the Last verify line on Sync status, and alerts on what it couldn't repair. The commands reach the running server through the admin listener, or run alone when no server holds the lock.

## User Stories

1. As the owner, I want payments made while my server was down to appear in the ledger and the sheet after it starts, so that an outage loses nothing.
2. As the owner, I want rows that came back through the sweep marked Via = sweep, so that the outage scene and I can see how they arrived.
3. As the owner, I want Razorpay's late retries of those deliveries to change nothing, so that each payment still has one row.
4. As the owner, I want the sweep to run at startup and every 10 minutes, so that a missed webhook is caught within minutes.
5. As the owner, I want the sweep to follow Razorpay's clock, not mine, so that clock drift can't open a gap.
6. As the owner, I want a crash mid-sweep to re-read at most one slice, so that nothing is skipped and nothing is stored twice.
7. As the owner, I want a quiet account's checkpoint to keep moving, so that it never looks stale.
8. As the owner, I want a new setup to start with the last 30 days of payments (configurable, 0 for none), so that a buyer's sheet begins with recent history.
9. As the owner, I want payments created in the same second, or more than 100 in two seconds, all listed, so that the paging can't drop any.
10. As the owner, I want the hourly re-list to bring in a late authorisation, an auto-refund, a fee set at capture and a refund reaching processed, so that the row catches up even when no webhook announces the change.
11. As the owner, I want an unchanged listed item to cost nothing, so that the re-list doesn't flood the queue.
12. As the owner, I want every swept item fetched in full before it's written, so that a listed state missing its refunds never makes a refunded payment look unrefunded.
13. As the owner, I want a heartbeat ping after every healthy sweep and none while Razorpay is paused or the sweep failed, so that an outside service tells me when the server or Razorpay's API is down.
14. As the owner, I want an alert when events reach us only through the sweeper, naming how to re-enable a disabled Razorpay webhook, so that a silent webhook is fixed quickly.
15. As the owner, I want the sweep paused with Razorpay when its key is revoked, so that it doesn't spin on 401s.
16. As the owner, I want `wsync backfill --from --to` to bring in older payments without touching the checkpoint, so that I can load history on demand.
17. As the owner, I want `wsync sweep` to run one sweep now, so that I don't wait ten minutes after fixing something.
18. As a buyer, I want the sheet checked against the ledger every hour, with missing or edited rows rewritten, so that a row someone deleted or changed comes back.
19. As a buyer, I want duplicated rows and rows with no matching payment reported rather than deleted, so that nothing of mine is removed silently.
20. As a buyer, I want the Daily summary checked too, so that the totals I rely on are proved.
21. As the owner, I want every payment Razorpay created in the last 30 days checked nightly against the ledger, with missing and stale payments repaired, so that the ledger is proved against the source.
22. As the owner, I want payments only in the ledger reported, not deleted, so that deleted test data or a key mix-up is visible.
23. As the owner, I want work in progress (a live job, a change during the run, a fee pending under an hour) skipped, so that verify never reports what is about to be fixed.
24. As a buyer, I want one line on Sync status like "Verified 03 Oct 02:00 IST: last 30 days, 7 payments, 0 differences", so that I can trust the sheet at a glance.
25. As the owner, I want an alert when verify finds differences it couldn't repair, or the same payment stale two nights running, so that a repeating fault is noticed.
26. As the owner, I want the nightly verify to run once per local date, catching up on start if the server was down at 02:00, so that no night is skipped.
27. As the owner, I want `wsync verify` (quick by default, `--full` for Razorpay too, `--repair`, `--days N`) to print each finding and its counts, so that I can check on demand.
28. As the owner, I want these commands to go through the running server when one is up and run on their own when not, so that two processes never write the database.
29. As the owner, I want a long verify not to time out the command, so that a 30-day run finishes.
30. As the owner, I want `wsync status` to show the last sweep per list and the last verify, so that I can see the safety net is working.
31. As the developer of P5, I want the sweeper and verify to work against the recorded list answers and a fake, so that the outage scenario can be scripted.
32. As the owner on Windows, I want the same behaviour there, so that the demo runs on my laptop.

## Implementation Decisions

**Modules** (`sweeper/` and `verify/` are new, per build-order.md):

- **Schema, migration `0004`**: `checkpoints` (source, account, list `payments`/`refunds`, `checkpoint_at` on Razorpay's clock, `last_sweep_at`, `last_sweep_stored`, `last_relist_at`, `relist_floor_at` (the list's first checkpoint, the earliest the re-list reads), `first_start_at` (Razorpay's now at the first start; older items are lookback history)), `verify_runs` (id, kind `quick`/`full`, started, finished, window days, local date, payments checked, findings count, repaired count, ignored events, outcome, the Sync status line, `by_hand` (a run started by hand, never counted as a nightly run)), `verify_findings` (run id, kind, source, account, payment id, detail, repair job id).
- **Config**: `[sweeper] initial_lookback_days` (default 30), `heartbeat_url` (optional, http/https); `[verify] days` (default 30), `nightly_at` (`HH:MM`, default `02:00`), `auto_repair` (default true); all modelled and validated. Alert kinds `silent_webhook`, `verify_differences`, `stale_twice` join P3's.
- **Razorpay lists** (sources): `list_payments(from, to, ...)` and `list_refunds(from, to, ...)` returning items and Razorpay's `Date` header time; both bounds always sent, Unix seconds, `from ≤ to` checked; `count=100`; paging by moving `to` to the oldest `created_at` seen + 1 and dropping ids already seen; a page that brings nothing new pages that two-second window with `skip`; the refunds `status` filter never used (G73). Same rate bucket and error classes as the fetch; a 401/403 pauses the Razorpay source and alerts.
- **Synthetic events** (Q145, Q146): stored by a function shared with the receiver's store: id `sweep:<entity id>:<state hash>` or `backfill:…` (origin `sweep` / `backfill`); type from the listed state (payment `authorized` → `payment.authorized`, `captured` or `refunded` → `payment.captured`, `failed` → `payment.failed`, `created` → not stored; refund `pending` → `refund.created`, `processed` → `refund.processed`, `failed` → `refund.failed`); fingerprint by Q117 with the entity's `created_at`; `body_sha256` NULL; payload through Q119's allow-list; `source_created_at` the entity's `created_at`; the configured account; payment key the payment id (a refund's `payment_id`); insert if absent, and only a new row enqueues a ledger job, with no prefetched state (Q147); no `deliveries` row. State hash per sources.md (payment: `status`, `captured`, `amount`, `amount_refunded`, `refund_status`, `fee`, `tax`, `error_code`, `error_reason`, and `notes` when metadata keys are configured; refund: `status`, `amount`, `speed_processed`), canonical JSON, SHA-256.
- **The sweeper**: per list, with no checkpoint start `initial_lookback_days` back (Q157); slices of up to 6 h from the checkpoint to Razorpay's now − 60 s; each requested as `from = start − 600`, `to = end`, keeping items with `created_at < end`; every page stored, then the checkpoint set to `end` in its own transaction; empty slices advance. Runs at startup after a random 0-30 s delay and every 10 minutes; skipped while Razorpay is paused. The hourly re-list: payments created in the last 7 days and refunds in the last 21, every page, through the same synthetic-event store (a changed state hash is a new id). Backfill (Q156): list payments created in a range, store as `backfill:` events, never touch a checkpoint. After a pass that finished every due slice (and the re-list when due): POST the heartbeat URL, 10 s timeout, failure logged (Q148). Silent webhooks (Q149): alert when a sweep stores a payment event whose entity is more than 10 minutes old, or when no delivery has arrived for 24 h while sweeps store events.
- **Verify, ledger against sheet** (Q150): one `getByDataFilter` read of the Payments and Daily summary tabs' values by tab id (P3's client and stored tab ids); for each ledger payment, the row P3's encoder would write, compared cell by cell over the configured columns; Daily summary rows recomputed from the ledger and compared; findings `sheet_missing`, `sheet_differs`, `sheet_duplicate`, `sheet_foreign`, `summary_differs`; repair: a mirror job (duplicates and foreign rows are reported only).
- **Verify, Razorpay against ledger** (Q151): list payments created in the last `days` and refunds created in that window + 21 days; attach processed refunds to their payment; map with P2's mapping; compare the business columns, excluding the card network; findings `missing`, `stale`, `orphaned`, plus the ledger's `not_found` and unknown-columns rows listed; repair: a ledger job that fetches.
- **Skips** (verify.md): payments with a live job, changed since the run started, or under an hour old with the fee pending.
- **Runs and schedule** (Q153, Q154): quick (sheet) on the hour; full (both checks) once per local date at the first valid `nightly_at` at or after the scheduled time, caught up at start if missed; `auto_repair` repairs after full runs, `--repair` on demand; every run and finding stored; the run's line stored and a status-only flush requested so Sync status shows it; the same payment `stale` on two consecutive full runs → `stale_twice` alert; findings left unrepaired → `verify_differences` alert with counts.
- **Commands and admin** (Q152): `wsync sweep`, `wsync backfill --from YYYY-MM-DD --to YYYY-MM-DD` (local dates), `wsync verify [--full] [--repair] [--days N] [--no-wait]`. With the lock held: `POST /admin/sweep`, `/admin/backfill`, `/admin/verify` answer 202 with a run id; `GET /admin/runs/{id}` reports progress and the result; the CLI polls and prints the findings. Without a server: the command runs in-process under the lock; repair jobs wait for the next `serve`. `wsync status` and `/admin/status` add last sweep per list and the last verify line.
- **Logs**: one line per slice, re-list, heartbeat and verify run with counts; never emails, keys or the spreadsheet id.

## Testing Decisions

**What a good test is here.** It drives the sweeper and verify from outside and asserts only on what an outsider sees: events stored with their origin, jobs created, `payments` rows after the ledger runs, checkpoints, `verify_runs` and `verify_findings`, the fake sheet's state, Sync status, the alert and heartbeat POSTs, and command output. Never private helpers. Razorpay's lists are answered by respx from the recorded list answers (`tests/recordings/razorpay/api/list/`), the fetch from P2's recordings, Google by P3's fake Sheets; time is moved with time-machine.

**The seam** (one, the highest): **the running app's background loops and the admin/CLI commands**, i.e. the sweeper and verify started by the app's lifespan (or invoked through the admin listener and as `wsync` subprocesses), against respx-faked Razorpay and P3's fake Sheets, with P2's ledger stage and P3's flusher running so a swept payment is followed all the way to the sheet. The outage story is the tracer: payments exist only at the fake Razorpay; the app starts; they reach the ledger and the sheet with Via = sweep; a late delivery then changes nothing.

**Modules tested:** the list calls and paging, synthetic events, the sweeper and re-list, heartbeat and silent-webhook alerts, both verify checks, skips and repair, schedules, the commands and admin runs, migration `0004`, config.

**Prior art:** P2's ledger tests with recorded Razorpay answers, P3's flusher tests against the fake Sheets and its admin/CLI subprocess tests, `time_machine.move_to(..., tick=False)`.

## Out of Scope

- The disputes poll, independent totals, the horizon alert and full backfill (Stripe concepts), `wsync replay`, the subscribed-types check (`doctor`, P6).
- Crash points, the five scripted scenarios, the duplicate storm and burst load checks, the full FakeRazorpay (P5).
- `wsync demo`, `doctor`, the weekly real run, the README and its honest-limits section (P6).
- Admin pages and tokens, Stripe, Postgres.

## Further Notes

- Same-second tie order in Razorpay's lists is still unknown (S-Q3a); the paging rule doesn't depend on it.
- Razorpay's list answers in test mode took 1-2 s for 7 items; a full 100-item page wasn't timed.
- The heartbeat URL answers 200 even when the check id is wrong; `doctor` (P6) checks for the body `OK`.

## Assumptions

**Defaulted by the owner to the recommendations**

- A1 [CLIENT] P4's scope as Q144: migration `0004`, lists, sweeper and re-list, heartbeat, silent-webhook alert, `sweep`/`backfill`/`verify`, hourly and nightly verify with repair, the Last verify line, verify alerts, status; later: disputes poll, independent totals, horizon and full backfill, replay, crash points, Stripe.
- A2 [CLIENT] A late delivery after a sweep is a new event that changes nothing; the outage caption says so (Q145).
- A3 [HARD-TO-CHANGE] Synthetic events as Q146.
- A4 [HARD-TO-CHANGE] No prefetched states for Razorpay; every swept job fetches (Q147).
- A5 [CLIENT] Heartbeat as Q148.
- A6 [CLIENT] Silent-webhook alert, both rules (Q149).
- A7 [HARD-TO-CHANGE] Verify ledger-against-sheet by re-encoding each row with P3's encoder (Q150).
- A8 [HARD-TO-CHANGE] Verify Razorpay-against-ledger from the payments and refunds lists, card network excluded (Q151).
- A9 [HARD-TO-CHANGE] Commands through the admin listener with 202 and run polling, or alone under the lock (Q152).
- A10 [CLIENT] The Last verify line and its wording (Q153).
- A11 [CLIENT] Schedules as Q154; auto-repair after nightly runs.
- A12 [CLIENT] Alert kinds `verify_differences`, `stale_twice`, `silent_webhook` (Q155).
- A13 [CLIENT] Backfill as Q156; first start as Q157.

**Decided internally**

- A14 [INTERNAL] Lists return Razorpay's `Date` time alongside items; the adapter's `_get` keeps the headers it needs — the checkpoint is on Razorpay's clock.
- A15 [INTERNAL] The synthetic-event store is shared with the receiver's, so both fill `events` the same way.
- A16 [INTERNAL] One checkpoint per list (`payments`, `refunds`), each with its own slice walk.
- A17 [INTERNAL] A verify run's line is stored on `verify_runs`; the flusher reads the latest when it writes Sync status, and a run requests a status-only flush.
- A18 [INTERNAL] "Once per local date" is read from `verify_runs.local_date` of full runs, so it survives restarts.
- A19 [INTERNAL] Admin runs are tracked in memory by id while the server lives; the result is in `verify_runs` and `checkpoints` for anything durable.
- A20 [INTERNAL] Backfill dates are local dates in `service.timezone`, converted to Unix seconds.

**Taken as given**

- A21 The owner's test account has about 7 payments and 2 refunds; tests use the recordings, never live calls.
- A22 One seam, the app's loops and commands with Razorpay faked by respx and Sheets by P3's fake, is the owner's default.

## Decisions made during the build

- #38: `list_payments`/`list_refunds` take `writer` and `pauses` as required keyword arguments (a list runs outside any job) and return `Listed` (items + `source_now` from the first page's `Date`) or a `Failure`; a missing `Date` is a retry failure.
- #38: paging falls back to `skip` within the two-second window whenever a full page cannot move `to` lower.
- #38: `queue.pause()` stores, caches and alerts a pause once; `Pauses.stored()` added and used by `fail()`.
- #38: an empty `heartbeat_url` means no heartbeat; `nightly_at` must be exactly 24-hour `HH:MM`.
- #38: migration 0004 constrains only `kind` (`quick`/`full`) and `list` (`payments`/`refunds`); `outcome` and the finding `kind` are free text.
- #39: `store_event` in the receiver's store is shared by deliveries and synthetic events; the adapter has `describe_listed` and `state_hash` (whose `notes` holds only the configured metadata keys).
- #39: each slice is requested up to start + 6 h, kept to `created_at < end`, `end` capped at the answer's `Date` − 60 s (Razorpay's clock without an extra call); a slice's events are stored in one transaction, the checkpoint moved in a separate one.
- #39: the sweeper uses the fetch's adapter, so lists and fetches share one rate bucket; `sweeper` sits in the receiver/ledger/sources import layer; it is stopped first on shutdown.
- #39: a list's first checkpoint is Razorpay's `Date` (from a one-second list call, once per list) minus `initial_lookback_days`; `last_sweep_at` stays NULL until a list's first slice is stored, and that first slice is read without the 10-minute overlap (so lookback 0 stores nothing older than first start).
- #39: each walk reads Razorpay's now from a one-second list call before its first slice; each slice's end is min(start + 6 h, that now − 60 s), sent as `to`.
- #41: the flusher's batch runner accepts a status-only flush (no jobs), skipped while the sheet is paused; the run line reads `Sheet checked 02 Oct 21:00 IST: 7 payments, 0 differences`; quick runs store `window_days` 0 and `outcome` `clean`/`differences`; no run row when the sheet is paused, never flushed, or the read fails (logged); `summary_differs` is repaired by a mirror job for one payment from that day; `webhook_sync.verify` shares the sweeper's import layer; the fake Sheets supports `deleteDimension`.
- #40: each list's first re-list is due an hour after its checkpoint is created; the first-start lookback walk never raises the silent alert; only the 10-minute slices count for the silent rules; the old-payment rule skips payments a delivery already brought; "no delivery for 24 h" counts from the first stored event when none ever arrived.
- #40: `app.state.sweeper` is the `Sweeper` (with `sweep` and `backfill`), the task is `app.state.sweep_loop`; backfill lists payments only, since each job fetches the payment with its refunds.
- #40: `checkpoints.relist_floor_at` (the list's first checkpoint) floors the hourly re-list, so a lookback of 0 stays empty; the sweeper logs `sweep pass ended` after each pass (silence check and heartbeat done).
- #42: a full run stores one `verify_runs` row holding both checks' findings; "(N ignored events)" appears only when N > 0; while Razorpay is paused the nightly run checks only the sheet and is stored as quick; `verify_differences` alerts come from full runs only; the refunds list reaches 21 days past the payments window; a flagged row Razorpay doesn't list is `not_found`, one it lists is `stale`; finding kinds `missing`, `stale`, `orphaned`, `not_found`, `unknown_columns`.
- #42: a payment that verify repairs as `missing` gets a blank Via (the repair job names no event; P2's A13 leaves `via` unchanged), a design gap rather than a contradiction.
- #40: `checkpoints.first_start_at` (Razorpay's now at the first start) separates lookback history from the silent-webhook rule, so a first walk cut short and resumed never alerts on its history; the spec's 0004 schema line gains `relist_floor_at` and `first_start_at`.
- #42: verify skips a listed payment past the payments checkpoint only while the sweeper moved it within 2 x SWEEP_EVERY; the start-up catch-up waits up to 2 minutes for both checkpoints to reach the start time (2-minute tolerance); a failed night is retried every 10 minutes (or Retry-After) until a full run is stored or the local date ends.
- #42: a night that keeps failing is given up at its local date's end with one WARNING log line and no alert (alert kinds are fixed by Q155/A12; a 'verify failed' alert would be the owner's call).
- #42: each nightly pass runs in its own try (logged, retried after RETRY_EVERY); the first-sweep wait's limit is on the event loop's clock; the shared test config sets nightly_at 23:59 so only test_verify_full.py runs the nightly verify.
- #43: a sweep by hand never pings the heartbeat; `Sweeper.sweep` runs one at a time, so a hand sweep and the loop can't move a checkpoint backwards; a hand full verify repairs only with `--repair`; `--days` without `--full` is refused (exit 2 / 422); `--no-wait` applies only with a server running; `verify` exits 0 on a finished run (differences or not) and 1 when it couldn't check; hand runs live in memory under random hex ids; the commands need a database `wsync serve` already created.
- #43: a run started by hand is stored with `verify_runs.by_hand = 1` and never counts as the night's run (A18 read as nightly runs only), for `stale_twice`, or for `verify_differences`; the admin listener answers 503 to new runs once shutdown has begun.
