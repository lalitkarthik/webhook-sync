# Changelog

## P6: demo, doctor, the weekly real run and the README — 2026-10-05

- `wsync doctor` checks a setup end to end (config, secrets, the Razorpay key and mode, Razorpay's webhook, the clock, the sheet, the database), prints one line per check and exits 0 unless one fails; `--url` posts an empty body to the public webhook URL and fails unless the receiver's own 400 comes back. It changes nothing.
- `wsync demo reset`, `outage`, `duplicates` and `verify` run the video's scenes against Razorpay test mode, paying test orders in a headless browser from the optional `demo` extra. Every scene refuses without an `rzp_test_` key and a spreadsheet on `[demo] spreadsheet_ids`; `reset`, `outage` and `duplicates` also refuse while a server runs.
- Once a week CI pays two test orders on the real Razorpay with the receiver stopped, sweeps them in and runs `verify --full`, failing on any finding; `python scripts/set_env.py github` sets the job's test-mode secrets without printing them.
- `README.md` states each promise beside the test that proves it, with the measured numbers, the limits and the credits; `THIRD_PARTY_NOTICES.md` credits the ported code.
- The sweeper now stores every payment when more than 100 are created inside two seconds, each sweep slice logs what it listed and stored, and the verify skill's launcher writes `.verify/port`.
- Razorpay's test-mode payments list showed new payments 28 to 54 minutes late on 5 October 2026. A sweep by hand can now ask for the re-list (`POST /admin/sweep` with `relist`), and `demo outage` does so and waits up to two hours for a late-listed payment.

## P5: prove nothing is lost or written twice — 2026-10-04

- `WSYNC_CRASH_AT=<name>` makes `wsync serve` exit with code 86 at a named place (`after_event_commit`, `after_sheet_write_before_done`, `mid_slice`); it is refused without an `rzp_test_` key or for an unknown name, and logs one warning when armed. Restarting on the same database loses nothing and writes nothing twice.
- The test suite now proves it: five scripted scenarios (the duplicate storm, the outage and catch-up, the late authorisation, Sheets answering 429 while a person edits the sheet, the partial and failed refund) each end with the nine invariants, and three crash tests kill and restart the real server.
- `uv run pytest -m load` runs the load checks: 1,000 concurrent replays of one delivery are answered 2xx or 429, never 503, with the server's own p99 at 0.8 s on the laptop, and 200 payments reach the sheet in at most three Sheets calls a flush. `synchronous=FULL` stays.
- The tests run on Windows once a week; no behaviour of P1-P4 changed, and no bug was found in it.

Measured on 2026-10-04 by `uv run pytest -m load`, the final run of the P5 build (PR #58), on the development laptop: an Acer Nitro AN515-58 (Intel Core i7-12650H, 16 GB of RAM, NVMe SSD, Windows 11), with `synchronous=FULL` and the local fakes. The storm: 1,000 of 1,000 deliveries answered at once, 72 x 200, 1 x 202, 927 x 429, 0 x 503, the refused sent again under the cap; server-side p99 0.79 s, client-side p99 4.3 s (printed, never asserted). The burst: 200 payments mirrored in 324 s by 104 flushes making 314 Sheets calls, 2 of them creating the tabs. Q172 and Q173 of the grilling log hold the earlier runs and the range across them.

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
