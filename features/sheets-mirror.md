# Sheets mirror

Each ledger change is copied to the buyer's Google Sheet: a Payments tab (one row per payment,
updated by key), a Daily summary tab and a Sync status tab, written in batched flushes. When
every write would fail (a refused key, a missing tab, a changed header, rows moved during a
write) the sheets sink pauses with a reason, alerts once, and waits for `wsync resume sheets`.

## Sub-features
- `mirror-job` — a real ledger change queues one mirror job for `sheets` per payment (coalesced: never two waiting).
- `mirror-flush` — the flusher writes up to 200 waiting jobs in one batch every 3 s, reading the tabs by id, appending new keys after the last row, then checking what it wrote.
- `mirror-tabs` — Payments, Daily summary (per local day and currency) and Sync status (`Label | Value`, rewritten each flush).
- `mirror-pause` — a pause-class failure pauses `sheets` with a reason ("Google refused the service-account key", "tab missing", "layout changed", "sheet changed during write"); its jobs wait without spending attempts.
- `mirror-alert` — a new pause posts one message to `[alerts] url` (Slack or Discord), at most one per kind an hour; with no URL, a warning log line.
- `mirror-resume` — see status-and-resume.md.

## How to get to it (user POV)
- The buyer opens the sheet named by `[sinks.sheets] spreadsheet_id`.
- The owner reads `wsync status` (`sheets (sink): paused since ...: <reason>`, `last flush: ...`).
- An alert arrives in the configured Slack or Discord channel.

## Driving it with the helper and wsync
Preconditions: launched and Doctor `OK`. Offline, Google's token endpoint is a fake that refuses the key, so only the job and the pause are drivable; no sheet is written.
- **Mirror job queued.** Run `<helper> deliver Tj9sMcl7hE9PaT`, then `<helper> sql --wait 15 "SELECT stage, sink, status, payment_key FROM jobs WHERE stage = 'mirror'"`. One job: `mirror`, `sheets`, `waiting`, `pay_Tj9rKBOXWcsMeE`.
- **Pause on a refused key.** Run `<helper> sql --wait 20 "SELECT name, state, reason FROM sinks WHERE state = 'paused'"`. `sheets`, `paused`, `Google refused the service-account key`. `cat .verify/run/upstreams.log` shows `"POST /token HTTP/1.1" 400`.
- **Jobs kept.** Run `<helper> sql "SELECT status, attempts, last_error_class FROM jobs WHERE stage = 'mirror'"`. `waiting`, 0, `pause` (a pause refunds the attempt).
- **Status shows it.** Run `wsync status --config .verify/run/webhook-sync.toml`. Lines `sheets (sink): paused since <time>: Google refused the service-account key`, `jobs: ledger 0 waiting, 0 dead; mirror 1 waiting, 0 dead`, `last flush: never`.
- **No alert URL.** Run `grep '"alert": "paused"' .verify/run/serve.log`. A warning line `alert not sent, no [alerts] url: webhook-sync paused: sink sheets paused: Google refused the service-account key` (the verify config has no `[alerts] url`, so nothing is posted).

## Gotchas
- A real flush needs the owner's throwaway sheet and service-account key from `.env`; verification never uses them. Payments, Daily summary and Sync status contents, the post-write check and the other pause reasons are proved by the in-process tests against FakeSheets (`tests/fakes/sheets.py`, contract-checked against `tests/recordings/sheets/`).
- After a resume the next flush pauses again within about 3 s, because the fake always refuses the key.
- `last flush` stays `never` offline: it is set only by a flush that wrote.
