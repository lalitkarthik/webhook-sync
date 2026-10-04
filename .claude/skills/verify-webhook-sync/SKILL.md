---
name: verify-webhook-sync
description: Launch, drive and observe webhook-sync the way a user does, and capture evidence. Used by the feature-loop flow check and auditors. Not run before commits.
---

# Verify webhook-sync

Kind: an HTTP service with a CLI. Surfaces: the public listener (`POST /webhooks/razorpay`,
`GET /health`), the loopback admin listener (`GET /admin/status`, `POST /admin/resume/{name}`),
the `wsync` commands (`serve`, `status`, `resume`, `--help`, `--version`), and files: the config
TOML in, the SQLite ledger `webhook-sync.db` and the JSON log lines on stderr out. No web pages.
The Google Sheet is a mirror this skill cannot reach offline (see Gotchas). Feature map:
`features/README.md`. Evidence goes to `$EVID` (the folder the orchestrator gave you); never
delete it.

All commands run from the repo root in Git Bash, each as a plain literal command (the
harness refuses a command whose name is a variable). On Linux or macOS write `.venv/bin/python`
for `.venv/Scripts/python.exe` and `.venv/bin/wsync` for `.venv/Scripts/wsync.exe`. If `.venv`
is missing, `uv sync` once (the orchestrator does this).

The helper `.claude/skills/verify-webhook-sync/wsyncv.py` (below, "the helper") does launch,
doctor, deliver, sql and cleanup. Run it with the venv's python, never `uv run`: on Windows the
detached server would die with uv's job object.

## Launch

```bash
.venv/Scripts/python.exe .claude/skills/verify-webhook-sync/wsyncv.py launch
```

What it does, so nothing is hidden:

- Port: a free one from the OS for the public listener (also in `.verify/port`); free ports for the admin listener and the fakes.
- Data: wipes and recreates `.verify/run/` only: `webhook-sync.toml` (test mode, account
  `acc_Tj5AnsI7BH2aLR` as in the recordings, `retry_profile = "demo"`, `api_url` at the fakes,
  sheet `fixture-spreadsheet-id-0001`, which is also on the `[demo]` list), the database under
  `.verify/run/data/`, `serve.log` (the server's JSON log lines) and `upstreams.log` (every call
  the server made to the fakes). The ports are one number per file: `.verify/port` (public),
  `.verify/run/admin_port`, `.verify/run/upstreams_port`; use them as `$(cat .verify/port)`
  (the harness refuses `source`).
- Secrets: the fixture ones from `tests/support.py`, never `.env` (no `.env` sits next to the
  config, and real `RAZORPAY_*`/`GOOGLE_*` variables are dropped). The server reaches Google
  through the fakes, its `HTTPS_PROXY`, trusting only their made-up authority (`SSL_CERT_FILE`).
- Fakes (`tests/fakes/over_http.py`, the tests' own FakeRazorpay and FakeSheets over HTTP):
  Razorpay's API answering `GET /v1/payments/{id}` with the recorded fetch
  (`tests/recordings/razorpay/fetch/payment-*-<id>.json`, else the recorded 400 "does not
  exist"), a payment's refunds pages, and the payments and refunds lists, empty: an account
  with no payments; Google's token endpoint answering 400 `invalid_grant`. Nothing leaves
  127.0.0.1.
- Process: one detached tree, top PID in `.verify/pid`: a supervisor (it hosts the fakes) whose
  child is the installed `wsync serve --config .verify/run/webhook-sync.toml`. On Windows the
  tree is python (venv shim) > python > wsync.exe > python > python; the real server's PID is in
  `serve.log`'s "Started server process [N]" line.
- Ready when it prints `ready: PID <n> (.verify/pid)` and the URLs: it waited for `/health` and
  `/admin/status` to answer 200. It refuses to start a second instance while `.verify/pid` is
  alive (exit 1: run Cleanup first). On failure it prints the tail of `serve.log`.

Variant: `... wsyncv.py launch --demo-fail-first 2` runs `wsync serve --demo-fail-first 2`.

## Doctor

```bash
.venv/Scripts/python.exe .claude/skills/verify-webhook-sync/wsyncv.py doctor
```

Read-only. Three lines, all `OK`, exit 0: `pid` (the PID in `.verify/pid` is alive), `health`
(`/health` on `.verify/port` answered `200 {"status":"ok"}`) and `owner` (`wsync status --config
.verify/run/webhook-sync.toml` printed `server: through the running server`, so the server
holding *this* data directory's lock is the one answering). Any `FAIL`: run Cleanup, then
Launch again; never drive an instance this run did not start.

## Drive

Per surface, with real commands. The feature files give each feature's steps.

- **Public HTTP: deliveries.** The helper re-signs a recorded delivery
  (`tests/recordings/razorpay/<event id>-<attempt>.json`) with the fixture webhook secret and
  POSTs it to `http://127.0.0.1:$(cat .verify/port)/webhooks/razorpay`, printing the request summary, the
  status and the JSON body:
  `.venv/Scripts/python.exe .claude/skills/verify-webhook-sync/wsyncv.py deliver Tj9sMcl7hE9PaT`
  (`payment.captured` of `pay_Tj9rKBOXWcsMeE`) → `"status": 202`, `{"result": "new"}`.
  Options: `--attempt 2` (Razorpay's retry, same bytes), `--header-event-id <id>` (same body
  under another id), `--bad-signature`. Event ids: `Tj9rs3245YOiVJ` payment.authorized,
  `Tj9sMcl7hE9PaT` payment.captured and `Tj9sMvWj9V6X75` order.paid (all `pay_Tj9rKBOXWcsMeE`);
  `Tj9sobwBnTgcRB` payment.failed (`pay_Tj9sm13R8h2378`); `Tj9h3likXEQFy4`, `Tj9h5SOW6XVf7g`,
  `Tj9h5yWwCUvsUn`, `Tj9tkWQjOGc6cJ`, `Tj9tnpcnWxNGXY`, `Tj9tw9WT2xqs9D`, `Tj9tzY1GfR6cQF`
  (`pay_Tj9gmEfed6kRqO`, captured then partly refunded).
- **Public HTTP: health.** `curl -s -w ' %{http_code}\n' http://127.0.0.1:$(cat .verify/port)/health` →
  `{"status":"ok"} 200`.
- **Admin HTTP.** `curl -s http://127.0.0.1:$(cat .verify/run/admin_port)/admin/status` → JSON with `sinks`,
  `jobs` and `last_flush`; `curl -s -X POST -H 'x-wsync-by: verifier'
  http://127.0.0.1:$(cat .verify/run/admin_port)/admin/resume/sheets` → `{"name":"sheets","result":...}`.
- **CLI.** The installed entry point, non-interactive:
  `.venv/Scripts/wsync.exe status --config .verify/run/webhook-sync.toml`,
  `.venv/Scripts/wsync.exe resume sheets --config .verify/run/webhook-sync.toml`,
  `.venv/Scripts/wsync.exe --help`, `.venv/Scripts/wsync.exe --version`. Exit codes: 0 done,
  1 failed, 2 refused (config).
- **Files: the ledger.** A read-only query, printed as JSON; `--wait S` polls up to S seconds
  until a row comes back (exit 1 if none), instead of sleeping:
  `.venv/Scripts/python.exe .claude/skills/verify-webhook-sync/wsyncv.py sql --wait 15 "SELECT payment_id, status, amount_minor, fee_minor, net_minor, via FROM payments"`.
  Tables: `events`, `deliveries`, `jobs`, `attempts`, `payments`, `sinks`, `sheet_state`,
  `alerts`, `rejection_counts`, `schema_version`.
- **Files: the log.** `grep '"delivery' .verify/run/serve.log`; calls to the fakes:
  `cat .verify/run/upstreams.log`.

## Evidence

Per acceptance criterion, one text file in `$EVID`, named `<AC id>-<what>.txt` (or `.json`):
the command, then its stdout, stderr and exit code. Capture with a redirect into `$EVID`
(`... > "$EVID/AC-1-1-deliver.json" 2>&1; echo "exit $?" >> "$EVID/AC-1-1-deliver.json"`) or
`| tee "$EVID/<file>"`. No screenshots: there is no UI.

Proof standards: drive the real path (HTTP to the running server, the installed `wsync`), not
internal functions or test-only routes. Capture the action and the resulting state: a delivery's
response *and* a second, read-only view (`sql` on `events`/`deliveries`/`jobs`/`payments`,
`wsync status`, or the log line). For side effects outside the database, `upstreams.log` shows
each fetch and token call the server made. Never put a real secret in evidence; the fixture
values are not secrets, but leave them out anyway.

## Cleanup

```bash
.venv/Scripts/python.exe .claude/skills/verify-webhook-sync/wsyncv.py cleanup
```

It kills only the tree in `.verify/pid` (Windows: `taskkill /T /F /PID <pid>`, the same as
`taskkill //T //F //PID $(cat .verify/pid)` from Git Bash; elsewhere SIGTERM to its process
group), prints `PID tree <pid>: gone`, then removes `.verify/pid` and
`.verify/run/`. It leaves `$EVID`, `.verify/setup-evidence/` and `.verify/port`. Never kill by
process name: other sessions run python too. Check with `tasklist //FI "PID eq <pid>"` (Windows)
or `ps -p <pid>` that the PID is gone. Run Cleanup after every failed attempt too.

## Gotchas

- The sheet is never written here: the fake token endpoint refuses the key, so the first flush
  pauses `sheets` ("Google refused the service-account key") and its mirror jobs wait. Writing
  the real throwaway sheet needs the owner's `.env` and is not part of verification; the Sheets
  behaviour itself is proved by the in-process tests against FakeSheets.
- `wsync resume sheets` works, then the next flush (about 3 s) pauses it again, since the fake
  key is always refused. Prove the resume by its own output and log line, not by a later status.
- Hard kill: Cleanup does not run the graceful shutdown (503 to new deliveries, releasing
  leases). The stop path is proved by the `posix` tests on Linux, not here.
- Use `sql --wait`, never `sleep`: the ledger job runs within about a second of a 202, a flush
  every 3 s while jobs wait.
