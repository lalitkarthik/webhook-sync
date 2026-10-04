# Changelog

## P5: prove nothing is lost or written twice — 2026-10-04

- `WSYNC_CRASH_AT=<name>` makes `wsync serve` exit with code 86 at a named place (`after_event_commit`, `after_sheet_write_before_done`, `mid_slice`); it is refused without an `rzp_test_` key or for an unknown name, and logs one warning when armed. Restarting on the same database loses nothing and writes nothing twice.
- The test suite now proves it: five scripted scenarios (the duplicate storm, the outage and catch-up, the late authorisation, Sheets answering 429 while a person edits the sheet, the partial and failed refund) each end with the nine invariants, and three crash tests kill and restart the real server.
- `uv run pytest -m load` runs the load checks: 1,000 concurrent replays of one delivery are answered 2xx or 429, never 503, with the server's own p99 at 0.8 s on the laptop, and 200 payments reach the sheet in at most three Sheets calls a flush. `synchronous=FULL` stays.
- The tests run on Windows once a week; no behaviour of P1-P4 changed, and no bug was found in it.

## P4: catch what webhooks missed, and prove the sheet is right — 2026-10-04

- The sweeper lists Razorpay's payments and refunds on Razorpay's own clock, at startup and every 10 minutes, so payments made while the server was down reach the ledger and the sheet with Via = sweep; an hourly re-list catches later captures, fees and refunds, and `heartbeat_url` gets one ping per healthy pass.
- A `silent_webhook` alert fires when the sweep finds payments no webhook brought, or none arrived for a day.
- Every hour the sheet is checked against the ledger, and every night the ledger against Razorpay for the last 30 days; what can be repaired is, the run's line shows on Sync status, and what can't raises `verify_differences` or `stale_twice`.
- New commands `wsync sweep`, `wsync backfill --from --to` and `wsync verify [--full] [--repair] [--days N] [--no-wait]`, through the running server or on their own; `wsync status` shows the last sweep and the last verify.

## P3: mirror the ledger to the Google Sheet — 2026-10-03

- `wsync serve` now runs the flusher: every 3 s (or at once at 200 jobs) it writes waiting payments to the Google Sheet in two reads and one write: a Payments tab with one row per payment, a Daily summary per local day and currency, and a Sync status tab saying whether all is healthy.
- Values are written exactly (currency decimals, real dates, text kept as text, emails masked, `pending` fees); a fresh sheet gets its tabs set up, renamed tabs keep working, and a sort during a write is caught and repaired.
- An unshared, deleted or full sheet, a missing tab or a changed header pauses the sheet with its reason; pauses survive restarts and are cleared by `wsync resume`, and `wsync status` shows every source and sink through a loopback-only admin listener.
- Optional alerts to a Slack or Discord webhook on pauses, dead jobs, a sheet changed during a write and an unknown currency, at most once an hour per kind, ids and counts only.

## P2: fetch each payment from Razorpay and keep its ledger row — 2026-10-02

- `wsync serve` now runs the ledger worker: each stored Razorpay event leads to one fetch of the payment's current state and one row per payment in the new `payments` table, however many events arrive and in whatever order.
- A row shows status, amount, processed refunds, fee and GST (pending until Razorpay sets them), net, method with one non-personal detail, the configured `notes` keys, the customer's email and the description; phone numbers, VPAs and card digits are never stored or logged.
- Only a real change bumps the row and queues one mirror job per configured sink, ready for P3's sheet flusher.
- A payment Razorpay doesn't know is flagged "not found at source" (or ends dead naming a possible mode or account mix-up); throttling, outages and timeouts retry with backoff, a revoked key pauses the source, and fetches stay under 10 a second.

## P1: receive Razorpay events exactly once, and queue their work — 2026-10-02

- `wsync serve` starts, refuses anything unsafe in one line naming the problem, runs one server per database and applies migrations itself.
- `POST /webhooks/razorpay` verifies each delivery and stores a genuine event exactly once, together with its ledger job, answering 202; repeats, replays and conflicts are counted, and every rejection gets its own reason.
- A database job queue with leases, retries with backoff, dead jobs, a reaper and crash recovery is ready for P2's ledger worker; stopping the server loses nothing.
- `--demo-fail-first N` answers 500 to each event's first N attempts, for the duplicate-storm scene, in test mode only.
