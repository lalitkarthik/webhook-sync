Status: planned

# P1: receive Razorpay events exactly once, and queue their work

Feature issue: #5

## Plain-language summary

This is the first piece of real code. It builds the front door: `wsync serve` starts, checks its settings and refuses to run if anything is unsafe, then listens for Razorpay's webhooks. Every genuine delivery is stored exactly once, together with a job saying "fetch this payment", in the same database transaction, so nothing can be acknowledged and then lost, and a delivery sent five times still makes one event. It also builds the job queue those jobs live in (leases, retries with backoff, dead after too many failures, recovery after a crash), ready for P2 to plug in the work that fetches the payment and writes the ledger. Nothing reaches the Google Sheet yet; at the end of P1 a delivery becomes an event row and a waiting job, and the tests prove each promise.

**Rests on:** [build-order.md](../design/build-order.md) step P1; [receiver.md](../design/receiver.md), [queue.md](../design/queue.md), [data-model.md](../design/data-model.md), [operations.md](../design/operations.md) (configuration, starting and stopping), [sources.md](../design/sources.md) (Razorpay); decision records [0004](../design/decisions/0004-queue-in-the-database-behind-an-interface.md) and [0007](../design/decisions/0007-one-server-process-per-database.md); grilling log Q14, Q15, Q37, Q48, Q70, Q71, Q76, Q83, Q84, Q86, Q91, Q106, Q109, Q111, and round 12, [Q115-Q121](../design/grilling-log.md); facts in [razorpay-receiver-facts.md](../research/razorpay-receiver-facts.md) (G59-G62). The design pages stay the source of truth; this spec holds only P1's behaviour and its Assumptions.

## Problem Statement

The owner is building a service that keeps one ledger row per Razorpay payment and must prove that nothing was lost or written twice. Razorpay delivers webhooks at least once, retries any non-2xx answer for 24 hours, gives up on an answer slower than 5 seconds, sends its event id in a header its signature does not cover, and marks nothing in a delivery as test or live. Without a receiver that verifies each delivery on its raw bytes, stores it exactly once together with its follow-up work, and a queue that survives crashes and stalls, every later promise (the live Payments tab, the outage catch-up, `wsync verify`) has nothing solid to stand on. There is no code yet.

## Solution

`wsync serve` reads `webhook-sync.toml` and secrets from the environment (or `.env`), refuses to start on anything unsafe and names what is wrong, takes an exclusive lock so only one server runs per database, and applies any pending migrations. It then serves `POST /webhooks/razorpay` and `GET /health` on the public port.

Each delivery passes the receiver's checks in order (shutting down, rate cap, size, content type, duplicate security headers, signature against any configured secret, a JSON object with an event id, the configured account). A genuine new event is stored in one transaction with its ledger job, or folded into the payment's live job by setting rerun; an ignored type is stored with its reason; a repeat is a counted duplicate; a different fingerprint under a known id is a counted conflict. Rejections are counted per reason per minute.

The queue offers its six operations, and a stage runner claims, heartbeats, completes or fails jobs, reaps expired leases with backoff, sends exhausted jobs to dead, reclaims jobs a crashed run left running, and releases unfinished leases at shutdown. In P1 no production stage is registered: jobs wait for P2's ledger worker. `--demo-fail-first N` answers 500 to the first N attempts of each event, after storing it, for the duplicate-storm scene and the recording spike, and only in test mode against an allow-listed spreadsheet.

## User Stories

1. As the owner, I want `wsync serve` to refuse to start when a Razorpay secret is missing or empty, naming the variable, so that a misconfiguration can never accept unsigned traffic.
2. As the owner, I want the service to refuse to start when `RAZORPAY_KEY_ID`'s prefix (`rzp_test_` or `rzp_live_`) does not match the configured mode, so that test events never reach live books and the other way round.
3. As the owner, I want an unknown key in `webhook-sync.toml` to stop the start with its name, so that a typo never silently changes behaviour.
4. As the owner, I want a secret written into the TOML to stop the start, so that secrets stay out of files that get committed or shared.
5. As the owner, I want a bad timezone name to stop the start, so that dates are never written in the wrong zone later.
6. As the owner, I want `[sources.stripe]` refused while Stripe is parked, so that I'm not misled into thinking Stripe works.
7. As the owner, I want my Razorpay account configured once as `acc_X` or the bare Merchant ID, so that I can copy it from the Dashboard either way.
8. As the owner, I want a second `wsync serve` on the same database refused, so that two servers can never write over each other.
9. As the owner, I want a server that died, even by a hard kill, not to block the next start, so that recovery needs no manual clean-up.
10. As the owner, I want migrations applied automatically and only once at start, so that upgrading needs no separate step.
11. As the owner, I want a database whose schema is newer than the code refused, so that an old binary can't damage newer data.
12. As the owner, I want the database and its lock kept under one data directory relative to the config file, so that the service behaves the same whatever directory I start it from.
13. As the owner, I want the real environment to win over `.env`, so that a cloud environment's values aren't overridden by a stray file.
14. As Razorpay, I want a genuine new delivery answered 202 within milliseconds, so that I stop retrying and never time out at 5 seconds.
15. As the owner, I want every genuine new event stored together with its ledger job in one transaction, so that the service can never answer 202 and lose the work.
16. As the owner, I want a delivery repeated any number of times to produce one event and counted duplicates, so that the duplicate storm ends as one row.
17. As the owner, I want a body replayed under a fresh event id treated as a duplicate, so that the unsigned event-id header can't be used to inject a second event.
18. As the owner, I want a different fingerprint under a known event id kept as the first event, answered 200, logged at error level and counted as a conflict, so that I can see it without the source retrying forever.
19. As the owner, I want new work for a payment whose job is running to set that job's rerun flag instead of making a second job, so that two fetches for one payment never race.
20. As the owner, I want new work for a payment whose job is waiting to fold into it, keeping its backoff, so that a burst of events makes one job.
21. As the owner, I want `order.paid`, `payment_link.*` and `payment.dispute.*` stored as ignored with a reason and answered 202, so that nothing disappears silently and Razorpay stops retrying.
22. As the owner, I want a dispute event to keep the payment it names, so that disputes can be built later without re-reading old bodies.
23. As the owner, I want a bad signature, a body over 1 MB, a non-JSON content type, a duplicated security header, a malformed body, a missing event id or the wrong account each answered with its own 4xx reason and never a 500, so that a misconfiguration is easy to diagnose.
24. As the owner, I want rejections counted per source and reason and written once a minute, so that a flood of junk costs one write a minute, not one per request.
25. As the owner, I want more than 50 requests a second from one source answered 429, so that a flood is stopped before any database work.
26. As the owner, I want a signature that matches any of several comma-separated webhook secrets to pass, so that rotating the secret doesn't drop the retries Razorpay still signs with the old one.
27. As the owner, I want a body without `account_id` accepted under the configured account with a warning, and a different `account_id` rejected as `wrong_account`, so that a stray webhook from another account can't enter my ledger and a missing field can't make Razorpay disable my webhook.
28. As a buyer, I want only the fields the service needs kept from each body, with email, phone, notes and payment-instrument details dropped, so that personal data isn't stored for no reason.
29. As the owner, I want SIGTERM or Ctrl-C to answer 503 to new deliveries, give running jobs up to 20 seconds, release unfinished leases with their attempt refunded, and free the lock, so that a restart loses nothing and Razorpay retries what we didn't take.
30. As the owner, I want jobs left running by a crashed run returned to waiting with the attempt counted at the next start, so that no job is stuck forever and a job that crashes its worker still ends dead.
31. As the developer of P2, I want a stage runner I plug a stage's work into, so that the ledger worker contains only its own logic.
32. As the developer of P2, I want claim to hand out a lease and a claim token, and complete to succeed only with the current token in the same transaction as my writes, so that a stalled worker's stale result is rolled back.
33. As the developer of P2, I want a worker that loses its lease to learn it from a heartbeat and stop without writing, so that two workers never both write.
34. As the owner, I want a failed job retried with backoff (production 5 s up to 8 h; demo 1 s to 16 s), multiplied by a random 0.5-1.5 factor and never zero, with a `Retry-After` honoured as a minimum, so that a struggling service isn't hammered and jobs that failed together spread out.
35. As the owner, I want a job to go dead after its attempts are used up (10 in production, 6 in demo) or at once on a `dead`-class error, with every attempt recorded, so that I can see why it failed and retry it later.
36. As the owner, I want a pause-class error to stop that source or sink and return the job to waiting with its attempt refunded, so that a revoked key doesn't burn through every job's attempts.
37. As the owner, I want the reaper to return expired leases one row at a time with backoff, so that one bad job can't stop the reaper for every other job.
38. As the owner presenting the demo, I want `wsync serve --demo-fail-first N` to store each new event and then answer 500 to its first N attempts, logging each attempt's arrival time, event id and body SHA-256, so that Razorpay's own retries make the duplicate-storm scene and the recording spike can measure them.
39. As the owner, I want the demo switch refused unless the key starts `rzp_test_` and the configured spreadsheet is in `[demo] spreadsheet_ids`, so that it can never fire against a client's live setup.
40. As the owner, I want each 500 the demo switch sends recorded in `deliveries` with its status code, so that the duplicate count on screen includes them.
41. As the owner, I want logs written as one UTF-8 JSON object per line with the event id, job id and payment key where known, and never secrets, signatures, raw bodies, emails or phone numbers, so that logs can be shared when asking for help.
42. As the owner, I want `GET /health` to answer 200 while the process is up, so that a platform or tunnel check can see it.
43. As the owner, I want CI to run lint, format check, typecheck and the tests on every push, so that a broken change is caught before it reaches the feature branch.
44. As the owner on Windows, I want the lock, the timezone check and the logs to work there too, so that the zero-setup demo runs on my laptop.

## Implementation Decisions

**Modules** (names from the package layout in [build-order.md](../design/build-order.md)):

- **config**: loads the TOML and the environment (python-dotenv; the real environment wins), validates and fails closed. Sections in P1: `[service]` (`public_listen` default `0.0.0.0:8080`, `admin_listen` default `127.0.0.1:8081` (read and validated but not served in P1), `data_dir` default `data` relative to the config file, `timezone` validated with `tzdata`, `retry_profile` `production` or `demo`), `[sources.razorpay]` (`mode`, `account`, `metadata_keys`), `[sinks.sheets]` (`spreadsheet_id` required as a string; the other keys accepted and validated for type only), `[demo]` (`spreadsheet_ids`). Sections P2-P4 own (`[sweeper]`, `[verify]`, `[retention]`, `[alerts]`) are accepted, so the example config works from day one. `--config` defaults to `./webhook-sync.toml`. Secrets: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` (comma-separated, newest first), all required and non-empty.
- **db**: the lock file (`filelock`, `fallback_to_soft=False`, 2 s timeout) at `<data_dir>/webhook-sync.lock`; the writer (one aiosqlite connection in manual-transaction mode behind an async lock, issuing `BEGIN IMMEDIATE` itself; SQLAlchemy Core builds statements; WAL, `synchronous=FULL`, `busy_timeout=5000`); a read-only reader connection; the migration runner (numbered `NNNN_<name>.sqlite.sql` files, each in one transaction, recorded in `schema_version`; refuse a schema newer than the code). The database is `<data_dir>/webhook-sync.db`.
- **Schema, migration 0001**: `events`, `deliveries`, `rejection_counts`, `jobs`, `attempts`, `schema_version`, with the columns in [data-model.md](../design/data-model.md). Keys: `events` primary key `(source, event_id)`; a unique index on `(source, body_sha256) WHERE body_sha256 IS NOT NULL`; the one-live-job unique index on `jobs (stage, sink, source, account, payment_key) WHERE status IN ('waiting','running')`. Later steps add their own tables in their own migrations.
- **sources**: an adapter interface (verify the raw request, parse the event: id, type, account, payment key, fingerprint, created time, handled or ignored with a reason, stripped payload) with one implementation, Razorpay. Stripe stays parked: no adapter, no route.
- **Razorpay adapter**: signature `x-razorpay-signature`, HMAC-SHA256 of the raw body against each configured secret, compared through the SDK's constant-time comparison; the body is decoded as UTF-8 first and a decode failure is `malformed`; every exception becomes a rejection reason, never a 500. Event id from `x-razorpay-event-id` (missing → 400 `missing_id`). Account from the top-level `account_id`: equal to the configured `acc_X` or 400 `wrong_account`; absent → the configured account and a warning. Primary entity by event-type prefix, read by name: `payment.dispute.*` dispute, `payment.*` payment, `refund.*` refund, `order.*` order, `payment_link.*` payment link. Payment key from `payload.payment.entity.id`, `payload.refund.entity.payment_id` or `payload.dispute.entity.payment_id`, stored whenever present, ignored events included. Fingerprint: SHA-256 of `type|primary entity id|top-level created_at or empty|mode` (Q117). `source_created_at` is the top-level `created_at` (Unix seconds → UTC), `NULL` when missing. Handled: `payment.authorized`, `payment.captured`, `payment.failed`, `refund.created`, `refund.processed`, `refund.failed`, `refund.speed_changed`; ignored with a reason: everything else, `order.paid`, `payment_link.*` and `payment.dispute.*` among them (Q109).
- **Payload allow-list** (Q119): top-level `entity`, `account_id`, `event`, `contains`, `created_at`; per entity `id`, `entity`, `amount`, `amount_refunded`, `currency`, `status`, `refund_status`, `captured`, `order_id`, `payment_id`, `method`, `speed_requested`, `speed_processed`, `error_code`, `error_reason`, `created_at`. Everything else is dropped before storing.
- **receiver**: the FastAPI app with `POST /webhooks/razorpay` and `GET /health`; reads `await request.body()` once, enforcing 1 MB while streaming. The checks in the order of [receiver.md](../design/receiver.md); status codes exactly as its table (202 new, 200 duplicate or conflict, 400/413/415 rejections, 429 rate cap, 503 shutting down or database error). The one transaction through the writer: insert the event if absent; if inserted and handled, enqueue the ledger job (stage `ledger`, sink `ledger`) and set `events.job_id`; if inserted and unhandled, outcome `ignored`; if not inserted, look up the stored event by id or, failing that, by body hash, and record `duplicate` (same fingerprint, or a body-hash match) or `conflict`; always insert a `deliveries` row (result, status code, duration). A body-hash replay's delivery carries the stored event's id; the header's id is logged.
- **Rate cap**: an in-memory token bucket per source, 50 a second with a burst of 50.
- **Rejection counters**: in memory, per source and reason, flushed once a minute through the writer to `rejection_counts`, and once more at shutdown. No samples in P1.
- **queue**: the six operations with the statements of [queue.md](../design/queue.md): `enqueue` (inside the caller's transaction; insert, or on the live job set rerun when running, clear `prefetched`, update `caused_by_event_id`; `run_after` untouched), `claim` (lease 60 s, new claim token, attempts + 1, `claimed_by` = instance id and start time; skips paused sources and sinks), `heartbeat` (fenced by token), `complete` (fenced, in the work's own transaction; rerun → waiting with attempts reset), `fail` (fenced; writes an `attempts` row; by class `retry`, `dead` or `pause`), `reap` (one row at a time, backoff, `attempts` row `reaped`, dead when exhausted). Error classes and the backoff tables as queue.md: production 5 s, 30 s, 2 min, 10 min, 30 min, 1 h, 2 h, 4 h, 8 h, then dead (10 attempts); demo 1, 2, 4, 8, 16 s, then dead (6 attempts); each wait × a random 0.5-1.5, never zero; `Retry-After` a minimum, never clamped. In P1, "paused" is an in-memory flag per source and sink that the runner checks; the `sinks` table and `wsync resume` come with P3.
- **Stage runner**: given a stage name, sink, claim limit and a work function (job → done, or an error with its class and optional `Retry-After`), it loops: claim due jobs, run the work with a heartbeat every 20 s, complete or fail. Separately, the reaper every 30 s. At start, before serving, every job still `running` goes back to `waiting` with its attempt counted and an `attempts` row `reaped`. At shutdown: stop claiming, wait up to 20 s, then release unfinished leases (back to waiting, attempt refunded, `attempts` row `released`), recorded on a fresh transaction. P1 registers no production stage; `serve` starts the reaper and the runner machinery only.
- **One clock**: the current UTC time comes from one Python function and is passed into SQL as a parameter, never SQLite's `now`, so tests can move time.
- **Startup order** ([operations.md](../design/operations.md)): take the lock → validate config (fail closed) → refuse a newer schema → apply migrations → reclaim running jobs → start the public listener. A refusal prints one line naming the variable or key, and exits non-zero.
- **Shutdown**: one signal handler of our own (not uvicorn's) for SIGTERM and SIGINT: 503 to new deliveries → stop claiming → up to 20 s for running jobs → release leases → flush rejection counters → release the lock.
- **Demo switch**: `wsync serve --demo-fail-first N` (N ≥ 1). Refused at start unless `RAZORPAY_KEY_ID` starts `rzp_test_` and `[sinks.sheets] spreadsheet_id` is in `[demo] spreadsheet_ids`. A new event is stored exactly as normal; then, per event id, an in-memory counter answers 500 to the first N attempts (the first included), each recorded in `deliveries` with status 500 and logged with arrival time, event id and body SHA-256; attempt N+1 onwards gets the normal answer (200, duplicate).
- **Logs**: stdlib logging with a JSON formatter, one UTF-8 object per line; `event_id`, `job_id`, `payment_key` where known; never secrets, signatures, raw bodies, emails or phone numbers.
- **CLI**: `argparse`; the `wsync` console script with the `serve` command only in P1.
- **Project scaffolding**: a `src`-layout package `webhook_sync` managed with uv, `requires-python >= 3.14`, ruff (lint and format), pyright, pytest with an asyncio plugin, `respx` and `time-machine` as test dependencies; the commands in CLAUDE.md work. A GitHub Actions workflow runs lint, format check, typecheck and the full suite on Linux on every push and pull request. `webhook-sync.toml.example` matches the config example in operations.md.

## Testing Decisions

**What a good test is here.** It drives P1 from outside, through one of the three seams below, and asserts only on what an outsider can observe: the HTTP status, the rows in the database read through the read-only connection, the process's exit code and its first line of output, and log records. It never asserts on private functions, call counts or the shape of internal objects, so P2-P4 can refactor freely. Time is moved with `time-machine`, never slept. Every test uses a fresh temporary data directory and real SQLite.

**The three seams** (agreed with the owner):

1. **The HTTP app.** The app is built from a config object and a data directory and driven in process with httpx over ASGI, using Razorpay's documented sample bodies signed by fixture code with a test secret and marked `unrecorded` (Q113) until P0's recordings land. Covers checks 1-8 and every status code; the one transaction (event + job; rerun on a running job; folding into a waiting job); ignored types; duplicates; body-hash replays under a new id; conflicts; `wrong_account` and missing `account_id`; payload stripping; several secrets; the rate cap; rejection counters after a minute; the demo switch's 500s, their `deliveries` rows and the normal answer after N; 503 while shutting down.
2. **The queue interface.** The six operations and the stage runner with a fake stage, on a real SQLite file. Covers the one-live-job rule, rerun on completion, fencing (a stale token's `complete` matches nothing and its writes roll back), heartbeat after a lost lease, each error class, backoff ranges in both profiles, dead after N, `Retry-After` as a minimum, the reaper one row at a time, startup reclaim and shutdown release. The backoff table is pure, so it also gets one direct table test.
3. **Starting the process.** `wsync serve` as a real subprocess: each fail-closed refusal names its variable or key and exits non-zero; a second server is refused while the first holds the lock and starts once the first is killed; migrations apply once and a newer schema is refused; SIGTERM gives 503 to new deliveries, releases leases and frees the lock. The fast validation cases also run in process through the same startup function the subprocess calls.

**Modules tested:** config, db (lock, writer, migrations), the Razorpay adapter, the receiver, the queue and the stage runner, each through the seams above.

**Prior art:** none in this repository yet; these tests become the prior art for P2-P5. The patterns to follow are in [testing.md](../design/testing.md) (the invariants, the settle helper, crash points in a real subprocess) and the integration-first style of `/mp-tdd`.

## Out of Scope

- Fetching payments, the mapping, the ledger upsert and the ledger worker's work (P2); P1 enqueues ledger jobs that wait.
- The Sheets sink, the flusher, the Sync status and Daily summary tabs, `wsync resume`, the `sinks` table (P3).
- The sweeper, checkpoints, the re-list, `wsync verify` (P4).
- FakeRazorpay and FakeSheets replaying recordings, the five scenarios, crash points, load checks (P5).
- `wsync demo`, `doctor`, the weekly real run, the README, the GIF (P6).
- The admin port and pages, `/ready`, `/status`, admin tokens, CLI-to-server forwarding, alerts of any kind, rejection samples, the pruner, backups.
- Stripe, Postgres, several workers, disputes as a handled type.
- P0's recording spike (owner's laptop, issue #4).

## Further Notes

- P1 is built before P0's recordings exist. The facts P0 settles (G38, G48, G49, G62) only change P5's fakes and P6's footage; G38 cannot change P1's code, because dedupe is on the event id first and fingerprints, not bytes.
- Q115 changes the prototype's "without the tests" (Q114) for P1 and adds about 1-2 h; the 8 October GIF date already depends on P0 being done by 4 October.
- The cloud environment resolves Python to 3.14.0rc2; the scaffolding ticket checks whether a final 3.14 is installable and says so.
- The design pages were aligned with round 12 in the same planning branch: ports, the Razorpay and demo config sections, `wrong_account`, conflicts logged until alerts exist, the partial body-hash index, `payment_key` on ignored events, and the migration file name.

## Assumptions

**Answered by the owner**

- A1 [CLIENT] Every P1 ticket ships unit and integration tests for its own behaviour; P5 keeps the recording fakes, the scenarios, the crash points and the load checks (Q115).
- A2 [CLIENT] P1 is `wsync serve` on the public port: receiver checks 1-8 with the 50/s cap and rejection counters (no samples), the one transaction, the six queue operations, a stage runner with no production stage, fail-closed start, the lock, migrations, `--demo-fail-first`, JSON logs, CI. Out: the admin port, `/ready`, `/status`, admin tokens, CLI forwarding, alerts. A conflict is logged at error level and counted, with no alert (Q116).
- A3 [HARD-TO-CHANGE] Fingerprint: SHA-256 of `type|primary entity id|created_at or empty|mode`; the entity chosen by event-type prefix and read by name; `source_created_at` `NULL` when the top-level `created_at` is missing (Q117).
- A4 [HARD-TO-CHANGE] Secrets: `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` (comma-separated, newest first; any match passes), all required at start (Q118).
- A5 [HARD-TO-CHANGE] `events.payload` is an allow-list of top-level keys and named entity fields; email, contact, notes and instrument details are dropped (Q119).
- A6 [CLIENT] Demo switch: needs the `rzp_test_` key and `[sinks.sheets] spreadsheet_id` in `[demo] spreadsheet_ids`; the attempt counter lives in memory; the 500'd attempts are recorded in `deliveries` (Q120).
- A7 [HARD-TO-CHANGE] Account: `[sources.razorpay] account` required, `acc_X` or bare `X`, stored as `acc_X`; a body `account_id` that differs is 400 `wrong_account`; a body without one is accepted with a warning (Q121).

**Decided internally**

- A8 [INTERNAL] The retry profile is set in TOML, `[service] retry_profile` — operations.md supersedes Q29's environment variable.
- A9 [INTERNAL] Migrations are named `NNNN_<name>.sqlite.sql` (Q48), each in one transaction; `0001` creates only P1's tables; later steps add their own; a newer schema refuses to start — history only moves forward.
- A10 [INTERNAL] `data_dir` defaults to `data`, relative to the config file, holding `webhook-sync.db` and `webhook-sync.lock`; `--config` defaults to `./webhook-sync.toml` — same behaviour from any working directory.
- A11 [INTERNAL] `.env` is loaded with python-dotenv (BSD-3); the real environment wins; a secret in the TOML refuses to start — cloud values can't be shadowed.
- A12 [INTERNAL] The lock is `filelock`, no soft fallback, 2 s timeout — covers the Windows release lag (G12).
- A13 [INTERNAL] At start every `running` job goes back to `waiting` with its attempt counted and an `attempts` row `reaped`; attempts are counted at claim; shutdown release refunds one — one server per database makes every running job at start stale.
- A14 [INTERNAL] One clock: Python's UTC time, passed into SQL — lets `time-machine` drive leases and backoff.
- A15 [INTERNAL] Writer: SQLAlchemy Core statements on one aiosqlite connection in manual-transaction mode with `BEGIN IMMEDIATE`; WAL, `synchronous=FULL`, `busy_timeout=5000`; a separate read-only connection — ADR 0004, Q50, G11.
- A16 [INTERNAL] `payment_key` is stored whenever the body names a payment, ignored dispute events included — disputes come later without re-reading bodies.
- A17 [INTERNAL] A body-hash replay under a new id records a delivery with the stored event's id and result `duplicate`, and logs the header's id; the unique index is partial on `body_sha256 IS NOT NULL` — P4's sweep events have no body.
- A18 [INTERNAL] The rate cap is a token bucket per source, 50/s with a burst of 50 — the design names the rate, not the algorithm.
- A19 [INTERNAL] Ports default to 8080 (public) and 8081 (admin) — matches every tunnel recipe and issue #4.
- A20 [INTERNAL] No Stripe route or config while it is parked; the sources interface stays — Q103.
- A21 [INTERNAL] `argparse`, `tzdata`, stdlib logging with a JSON formatter — no CLI library is in the stack; `tzdata` makes the timezone check work on Windows.
- A22 [INTERNAL] Scaffolding: `src`-layout `webhook_sync`, uv, Python ≥ 3.14, the `wsync` script, ruff, pyright, pytest; CI on Linux on every push — CLAUDE.md's commands.
- A23 [INTERNAL] A missing `x-razorpay-event-id` is 400 `missing_id` — as the design says; G62 confirms on real deliveries.
- A28 [INTERNAL] In P1 a pause is an in-memory flag per source and sink that `claim` respects; the `sinks` table and `wsync resume` come with P3 — the cut's simple pause (queue.md), without P3's tables.
- A29 [INTERNAL] The config accepts the sections P2-P4 own (`[sweeper]`, `[verify]`, `[retention]`, `[alerts]`), so the example config validates from day one — unknown keys inside a known section still refuse to start.

**Taken as given**

- A24 Secret rotation: the old secret must stay listed for 24 h after a change; the README (P6) says so (G60).
- A25 Dashboard replays arrive under the same event id, so they are duplicates; nothing new is needed in the receiver (G63 checks test mode).
- A26 P0 does not block P1; tests use Razorpay's documented samples, marked `unrecorded`, until P0's recordings land.
- A27 The cloud environment's Python is 3.14.0rc2 until a final 3.14 is installable.
