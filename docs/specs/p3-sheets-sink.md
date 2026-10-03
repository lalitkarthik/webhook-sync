Status: implemented

# P3: mirror the ledger to the Google Sheet

Feature issue: #27

## Plain-language summary

After P2 the ledger has one true row per payment, and every change queues a job for the sheet. P3 builds the flusher that drains those jobs into the buyer's Google Sheet: a Payments tab with one row per payment, a Daily summary of gross, refunds, fees and net per day, and a Sync status tab that says whether everything is healthy. Each flush costs two reads and one write however many payments it carries, survives people renaming tabs or sorting rows, and when the sheet is unshared, deleted or full it stops, says why, alerts, and waits for `wsync resume`. Pauses now survive restarts, and a small local-only admin port lets `wsync resume` and `wsync status` talk to the running server.

**Rests on:** [build-order.md](../design/build-order.md) step P3; [sheets-sink.md](../design/sheets-sink.md) (the source of truth for the flush, encoding and failures), [ADR 0006](../design/decisions/0006-machine-owned-tabs.md), [ADR 0007](../design/decisions/0007-one-server-process-per-database.md), [ADR 0001](../design/decisions/0001-database-is-the-ledger-sheet-is-a-mirror.md), [queue.md](../design/queue.md) (pausing in the cut), [operations.md](../design/operations.md) (config, alerts, commands), [data-model.md](../design/data-model.md) (`sinks`, `sheet_state`, `alerts`), [edge-cases.md](../design/edge-cases.md) D3-D11, G1-G17, J6; grilling log Q19, Q21, Q23, Q62, Q63, Q87, Q90, Q92, Q95, Q97, Q100 and round 14, [Q132-Q143](../design/grilling-log.md); facts in [sheets-live-facts.md](../research/sheets-live-facts.md) (G67-G70), [gaps-google-python-razorpay.md](../research/gaps-google-python-razorpay.md) (G18-G25) and the recordings under `tests/recordings/sheets/`; the code P1 and P2 built ([p1](p1-receiver-queue.md), [p2](p2-fetch-ledger.md), "Decisions made during the build").

## Problem Statement

The owner's buyers live in Google Sheets, and the ledger is invisible to them. Mirror jobs from P2 pile up with nowhere to go. Writing to a sheet that people also use is where the studied repos broke: rows landing on the wrong payment after a sort, appends a thousand rows down, a renamed tab breaking every write, a failed read leading to every row being appended again, formulas and phone numbers mangled by user-entered parsing, and quota errors retried into a storm. A paused service that forgets its pause on restart, or a pause no one hears about, is as bad as no pause.

## Solution

A flusher runs as a batch stage on P1's stage runner: every 3 s when sheet jobs are waiting, or at once when 200 are, it claims up to 200, reads the tabs by id in one call (grid size, headers, key columns), loads each payment's current ledger row, builds one `batchUpdate` (updates without the key cell, appends with it after the last used row, the Daily summary days touched, Sync status), writes it once, re-reads the written keys, and completes every job in one transaction. Values are typed so nothing is re-parsed. On a fresh sheet it creates or adopts the three tabs. Failures are classed from the live answers: retry, pause with a reason, or dead for one bad row. Pauses are stored in `sinks`, alerted, shown by `wsync status`, and cleared only by `wsync resume`, which reaches the running server through an admin listener bound to loopback.

## User Stories

1. As a buyer, I want a Payments tab with one row per payment and the columns my bookkeeper expects, so that I can see every payment without logging into Razorpay.
2. As a buyer, I want a payment's row updated in place when it is captured or refunded, so that the sheet never shows the same payment twice.
3. As a buyer, I want new payments added straight after the last row, so that rows never land far down the sheet or on top of something else.
4. As a buyer, I want amounts as real numbers with the right decimals and dates as real dates in my time zone, so that filters, pivots and totals work.
5. As a buyer, I want text written exactly as it is (`=HYPERLINK(…)`, `+91…`, `007`), so that nothing is turned into a formula or loses its zeros.
6. As a buyer, I want Fee, GST on fee and Net to say `pending` until Razorpay sets the fee, so that I never read a zero fee as real.
7. As a buyer, I want customer emails masked as `a***@gmail.com` by default, so that the sheet can be shared inside my team.
8. As a buyer, I want one column per metadata key I configured (e.g. `order_id`), so that I can join the sheet to my own orders.
9. As a buyer, I want a Note column that says "not found at source" or names unknown columns, so that odd rows explain themselves.
10. As a buyer, I want a Daily summary with count, gross, refunds, fees and net per local day and currency, from succeeded payments only, so that I get correct totals without summing failed attempts.
11. As a buyer, I want a late payment to update its own day's summary row, so that past days stay correct.
12. As a buyer, I want a Sync status tab showing the last flush, rows written, jobs waiting, dead jobs and whether anything is paused, so that I can trust the sheet at a glance.
13. As a buyer, I want the header row and Payment ID column frozen, with a note explaining filter views, so that I know how to sort without breaking anything.
14. As a buyer, I want my own tabs left alone, so that my work is never touched.
15. As a buyer, I want renaming a tab to change nothing, so that I can name tabs as I like.
16. As a buyer, I want a sort of the Payments tab during a flush to be caught and repaired, so that no payment's values end up on another payment's row.
17. As the owner, I want 200 payments arriving together to cost a handful of API calls, not 200, so that Google's quota is never the bottleneck.
18. As the owner, I want a failed read to stop the flush and retry later, so that a glitch never appends every row again.
19. As the owner, I want a write whose reply was lost to be safe to repeat, so that retries never duplicate rows.
20. As the owner, I want Google's 429 to wait until the quota window resets, so that retries don't extend the throttling.
21. As the owner, I want an unshared or revoked sheet to pause the sink with the reason, keeping every job, so that nothing is lost and nothing is retried pointlessly.
22. As the owner, I want a deleted Payments or Daily summary tab, a missing spreadsheet, a changed header or a full sheet each to pause with its own reason, so that I know exactly what to fix.
23. As the owner, I want a deleted Sync status tab simply recreated, so that it never needs me.
24. As the owner, I want rows deleted by a person to cause a retry, not a pause, so that the next flush reads the sheet again and carries on.
25. As the owner, I want one bad cell to send only its payment's job to dead and the rest of the batch written, so that one odd value can't block the sheet.
26. As the owner, I want an unknown currency to hold back only that payment's row, dead with an alert, so that no amount is written 100 times off.
27. As the owner, I want rows moved twice in a row during writes to pause the sink with "sheet changed during write", so that a fight with a person editing the tab stops.
28. As the owner, I want a pause to survive a restart, so that a restart never quietly hammers a broken sheet or a revoked key.
29. As the owner, I want `wsync resume sheets` (or `razorpay`) to clear the pause whether or not the server is running, so that recovery is one command.
30. As the owner, I want `wsync status` to show sinks and sources, their pause reasons, jobs waiting and dead, and the last flush, so that I can see health even when the sheet is unreachable.
31. As the owner, I want the admin listener reachable only from the machine itself, so that nothing new is exposed to the internet.
32. As the owner, I want an alert when the sheet or Razorpay pauses, when new jobs go dead, when the sheet changed during a write, and on an unknown currency, at most once an hour per kind, so that I hear about trouble without being flooded.
33. As the owner, I want alerts to work with a Slack or Discord webhook URL, carrying ids and counts only, so that no customer data leaves the service.
34. As the owner, I want a missing or malformed `GOOGLE_SERVICE_ACCOUNT_B64` to stop the start with its name, so that a bad key is caught before the first flush.
35. As the owner, I want a fresh spreadsheet set up on the first flush and an existing matching tab adopted, so that setup needs no manual tab creation.
36. As the owner, I want a first-run tab with a different header paused as "layout changed" rather than overwritten, so that a buyer's tab is never clobbered.
37. As the developer of P4, I want Sync status to have a "Last verify" line and the flush to expose how it reads the sheet, so that verify can reuse both.

## Implementation Decisions

**Modules** (P1/P2's layout; `sinks/` and `admin/` are new, per [build-order.md](../design/build-order.md)):

- **Schema, migration `0003`**: `sinks` (name, kind `sink`/`source`, state `active`/`paused`, reason, since, `moved_in_a_row`), `sheet_state` (spreadsheet id, Payments/Daily summary/Sync status tab ids, deployment id, last flush at, rows written total), `alerts` (kind, last sent at, suppressed count). Existing pauses move from P1's in-memory set to `sinks`; the in-memory set stays as the claim-time cache, loaded at start and updated with every pause and resume.
- **Config**: `GOOGLE_SERVICE_ACCOUNT_B64` joins the required secrets, decoded at start and refused (naming it) unless it is a service-account key; never logged. `[sinks.sheets]` gains `columns` (default as Q136), `mask_email` (default true); unknown column keys refused. `[alerts] url` modelled (empty allowed). `admin_listen` must be a loopback address in the cut (Q133). `webhook-sync.toml.example` updated to match operations.md.
- **Sheets client**: the Sheets REST API v4 over async httpx with google-auth issuing the token (`spreadsheets` scope), not gspread; a 10 s read and 30 s write timeout. Calls: `spreadsheets.get` (tab properties), `spreadsheets.getByDataFilter` by `sheetId` with a field mask (grid size, header rows, key columns of Payments and Daily summary in one call), `spreadsheets.batchUpdate`. Classification from the live answers (Q141): 429 → retry with `window_start_time` + 60 s as the minimum; 5xx, timeouts, connection errors → retry; 401, 403 → pause; 404 → pause "spreadsheet missing"; 400 "No grid/sheet with id" → pause "tab missing"; 400 "after last row in grid" → retry; 400 naming `requests[N]` → that request's row; the cell-limit message → pause "sheet full".
- **Batch stage on the runner**: P1's stage runner gains a batch mode: claim up to N jobs of a stage, run one work call for all, complete them all in one transaction (or fail them all, or fail named ones dead and retry the rest). The flusher runs every 3 s while sheet jobs wait, or at once at 200; heartbeats cover the batch.
- **Encoding** (sheets-sink.md "Encoding values"): `updateCells` with typed values (`stringValue`, `numberValue`), never user-entered; dates as serials in `service.timezone` with an explicit `yyyy-mm-dd hh:mm:ss` pattern; amounts `Decimal(minor) / 10^exponent` with a format per exponent from ISO 4217 plus overrides, codes upper-cased; an unknown currency → that job dead with an alert; past 15 significant digits → text; cells cut at 50,000 UTF-16 code units ending `…[truncated]` (Q142); `pending` as text in Fee, GST on fee, Net while the fee is null; email masked as first character + `***` + domain (`***` when there is no `@`); Note "not found at source" and/or "unknown: <columns>". Headers fixed per column key; the zone's abbreviation when it has no daylight saving (IST), its name otherwise.
- **Tabs and first run** (Q140): with no tab ids stored, adopt a tab named Payments, Daily summary or Sync status whose header matches, pause "layout changed" if it differs, create a missing one (header, header note, frozen header row and key column); store the ids. Afterwards: missing Payments or Daily summary → pause "tab missing"; missing Sync status → recreate. Rows in the sheet with no ledger payment are left alone.
- **The flush** (sheets-sink.md "The flush", with Q141-Q143): claim → one read by tab id → any read failure fails every job with its class → check tabs and header (only our configured columns; extra columns to the right ignored) → map key → row (the last key row is the last used row; a duplicated key: the first is updated) → load each job's current ledger row and the Daily summary days its payment touches → one `batchUpdate`: updates without the key cell, appends with it after the last used row with `appendDimension` when the grid is too small, the touched summary rows (recomputed from the ledger for each touched day), Sync status rewritten whole → write once, splitting only past Google's 2 MB guidance, each part complete in itself → a 400 naming `requests[N]` sends that payment's job dead and writes the rest again → post-write check: re-read the keys of every row written; a moved key enqueues mirror jobs for both the expected and the found key, alerts, and increments `moved_in_a_row` (a second in a row pauses "sheet changed during write"; a clean flush resets it) → complete every job in one transaction and update `sheet_state`.
- **Daily summary** (Q137): rows keyed by local day, currency and fee currency; columns Day, Currency, Fee currency, Payments, Gross, Refunds, Fees, Net (before refunds), Fee pending; succeeded payments only (partly and fully refunded included); a payment's day is its `source_created_at` in `service.timezone`; refunds count on the payment's day; a fee-pending payment counts in Payments, Gross and Refunds and in Fee pending, not in Fees and Net.
- **Sync status** (Q138): label/value rows: Last flush, Rows written (last flush), Rows written (total), Sheet jobs waiting, Dead jobs, Sheet state, Razorpay state, Last verify (blank until P4), Deployment id.
- **Admin listener and CLI** (Q133, Q134): a second uvicorn listener on `admin_listen` (loopback only), stopped by the same single signal handler. `POST /admin/resume/{name}` clears a pause in `sinks` and the in-memory cache, and records who and when in the log; `GET /admin/status` returns sinks and sources with state, reason and since, jobs waiting and dead by stage, and the last flush. New commands `wsync resume <name>` and `wsync status`: when the lock is held, they call the admin listener; when not, they act on the database directly (status reads; resume updates `sinks`).
- **Alerts** (Q139): kinds `paused` (sink or source, with reason), `dead_jobs` (new since the last alert, counts by stage), `sheet_changed`, `unknown_currency`. Sent as one JSON POST to `[alerts] url` with both `text` and `content`, 10 s timeout, never retried beyond the next due time; each kind at most once an hour, the `alerts` row counting the suppressed ones and the next message including the count; ids and counts only; no URL → one warning log line. A dead-jobs check runs once a minute.
- **Logs**: one line per flush (jobs, rows updated, appended, summary rows, duration, outcome) and per pause, resume and alert; never emails, keys, the spreadsheet id or the service-account address.

## Testing Decisions

**What a good test is here.** It drives the flusher from outside: mirror jobs in the queue and payments in the ledger go in; what comes out is the sheet's state as the fake holds it (tabs, headers, rows, formats, frozen rows), the jobs' states, `sinks`, `sheet_state`, `alerts`, the alert POSTs and log records. Never private helpers or call order, except the number of API calls per flush, which is a promise (two reads and one write).

**The seam** (one, the highest): **the flusher running on the stage runner, against an in-memory fake Sheets service served through respx**, implementing exactly the calls the flusher makes (`spreadsheets.get`, `getByDataFilter`, `batchUpdate` with the request kinds used) and answering errors with the bodies recorded live in `tests/recordings/sheets/`. A contract test replays the recorded requests against the fake and checks it answers as Google did, so the fake can't drift from the recordings. This fake is the start of P5's FakeSheets. Admin and CLI behaviour is tested through the admin listener in process and `wsync resume`/`status` as real subprocesses; alerts through a respx-faked webhook URL.

**Modules tested:** the Sheets client, the batch runner mode, encoding, the flusher, tabs and first run, Daily summary, Sync status, `sinks`/pauses, the admin listener and CLI, alerts, migration `0003`, config, each through the seam above (encoding's pure rules may also run as a table test).

**Prior art:** P1's HTTP-app and subprocess tests, P2's ledger-stage tests on the runner with respx and recorded fixtures, `time_machine.move_to(..., tick=False)`.

## Out of Scope

- Rebuild in place, probes and automatic resume, the deployment-id check (the id is shown only), cell-count tracking and the 80 % warning, the OAuth fallback, "5 retry failures in a row pause".
- Verify and its Sync status line, the sweeper (P4); FakeSheets' full scenarios, crash points and load checks (P5); `doctor`, `demo`, README (P6).
- Admin pages and admin tokens; a non-loopback admin listener.
- A Dispute column, a Settled column, a refunds tab, net after refunds.

## Further Notes

- The live check (2026-10-03) on the owner's throwaway sheet: locale `en_GB`, time zone `Asia/Calcutta`; a date format without an explicit pattern follows the locale, so the pattern is always sent; reads of 70 in a burst hit 429 at the 67th and recovered at the next minute; the cell limit is 20,000,000 and counts empty grid cells; a failed batch applies nothing.
- Whether an unshared private sheet answers 403 or 404 is untested; both pause, with different reasons.
- Developer metadata could address rows without the post-write check, but only through the values API with a 30,000-character budget per sheet; the post-write check stays.

## Decisions made during the build

Settled by the implementers or in review, within what this spec and the design leave open:

- `sinks` holds one row per name with `kind` `sink` or `source`; every configured source and sink gets an active row at start, so `wsync status` lists them before anything pauses. A second pause keeps the first reason and time; resume sets `since` to the resume time.
- `columns` must include `payment_id` and refuses repeated keys. The CLI sends `x-wsync-by: cli` to the admin listener; the resume log line carries `target` and `by`. Commands detect a running server by taking the lock without waiting.
- Alerts: one JSON POST carrying both `text` and `content`, formatted `webhook-sync <kind>: <text>`. A failed POST still counts as that hour's send. A dead-jobs check that is not yet due skips without counting a suppression, so the next alert covers every job that died since the last one. Unknown-currency alerts are batched one per flush, sent only after the write succeeds.
- Sheets client: a key Google refuses pauses the sink; an unreachable token endpoint retries. A 401 drops the cached token, so the first call after `wsync resume` fetches a new one. A 200 whose body is not JSON retries; a 400 that names no request is dead for the whole batch.
- The batch runner's `Done.failed` maps a job id to its failure, so one bad row goes dead while the rest complete. Each appended Payments row is its own `updateCells` request, so a 400 naming `requests[N]` points to one job.
- First run creates the three tabs with sheet ids we choose, in one `batchUpdate`. Frozen key columns: Payments 1, Daily summary 3, Sync status 1. Sync status is a `Label | Value` tab. Amounts use the number format `0.00` (or their currency's decimals).
- Currency codes are upper-cased for the exponent lookup only; the Currency cell shows the code as the ledger stores it. A summary row in an unknown currency is left out, as its payments are. The Note joins its two parts with "; ". Cells for unknown columns are left as they are; only the Note names them.
- A mirror job with no ledger row completes without writing. A touched Daily summary key that no longer matches the ledger is set to zero rather than removed. The Day key is written as a date formatted `yyyy-mm-dd`.
- The deployment id is a uuid4 made on the first flush and stored in `sheet_state.deployment_id`; it is shown only (G17's check stays deferred).
- Dead jobs in Sync status count every stage plus the jobs this flush made dead; jobs waiting count sheet mirror jobs left after this flush claims its batch.
- The post-write check re-reads only the Payments key cells; Daily summary rows moved by a sort are not checked. If the check read fails, every job fails with that class (the next flush writes by key). `wsync resume` also resets `moved_in_a_row` to 0.
- User story 37's second half (a public read of the sheet for verify to reuse) is left to P4, which will shape it to verify's needs; the flush's read lives in `Flusher._read`. Sync status already carries the blank "Last verify" line.

## Assumptions

**Answered (owner accepted every recommendation)**

- A1 [CLIENT] P3's scope is migration `0003`, Google credentials and client, the flusher with the post-write check and repair, encoding, the three tabs, persistent pauses, `wsync resume`/`status`, and alerts; rebuild, probes, the deployment-id check, cell tracking, OAuth, verify's line and the 5-failures pause are later. Each ticket ships its own tests (Q132).
- A2 [HARD-TO-CHANGE] The admin listener is bound to loopback only, serving resume and status; the CLI uses it when a server runs and the database otherwise; no tokens in the cut (Q133).
- A3 [HARD-TO-CHANGE] Pauses are stored in `sinks` and survive restarts; only `wsync resume` clears them (Q134).
- A4 [HARD-TO-CHANGE] `GOOGLE_SERVICE_ACCOUNT_B64` only, required and validated at start; the sheet id only from the TOML (Q135).
- A5 [CLIENT] Payments columns as Q136, Settled dropped, Fee currency, `pending` text, masking on by default.
- A6 [CLIENT] Daily summary as Q137.
- A7 [CLIENT] Sync status as Q138.
- A8 [CLIENT] Four alert kinds, Slack and Discord in one payload, hourly per kind, a warning log without a URL (Q139).
- A9 [CLIENT] First run adopts or creates tabs; afterwards a missing Payments or Daily summary pauses and a missing Sync status is recreated; extra sheet rows are left alone (Q140).
- A10 [HARD-TO-CHANGE] Failure classes from the live answers (Q141); 50,000 UTF-16 units per cell (Q142); a moved row repairs both keys (Q143).

**Decided internally**

- A11 [INTERNAL] The Sheets client is async httpx with google-auth, not gspread — gspread is synchronous and hides the HTTP status.
- A12 [INTERNAL] P1's runner gains a batch mode for the flusher (claim up to 200, one work call, complete all in one transaction); the ledger keeps the single-job mode.
- A13 [INTERNAL] The in-memory pause set remains the claim-time cache, loaded from `sinks` at start and updated on every pause and resume.
- A14 [INTERNAL] "Twice in a row" is `sinks.moved_in_a_row`, reset by a clean flush.
- A15 [INTERNAL] Daily summary rows are recomputed from the ledger for each touched day, never incremented — idempotent like the rest of the flush.
- A16 [INTERNAL] Email masking: first character + `***` + domain; `***` without an `@`.
- A17 [INTERNAL] The dead-jobs alert check runs once a minute in the server.
- A18 [INTERNAL] Tests use an in-memory fake Sheets service through respx, held to the live recordings by a contract test — the start of P5's FakeSheets.
- A19 [INTERNAL] `webhook-sync.toml.example` and operations.md list the same columns; unknown column keys are refused at start.

**Taken as given**

- A20 The throwaway spreadsheet `SHEET_ID` is the one the weekly run and manual checks use; tests never call Google.
- A21 Version 1 is INR only, so Currency and Fee currency match in practice; the two axes stay for Stripe.
- A22 One seam, the flusher on the runner against the fake Sheets service, is the owner's default (questions defaulted).
