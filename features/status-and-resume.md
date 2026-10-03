# Status and resume

The owner sees each source and sink with its state, the jobs waiting and dead per stage, and
the last flush, and clears a pause, from the command line, whether or not the server is
running. With a server running, the commands go through its loopback admin listener.

## Sub-features
- `status-via-server` — `wsync status` while a server holds the lock: first line `server: through the running server`.
- `status-on-database` — `wsync status` with no server: first line `server: on the database: no server is running`.
- `status-no-database` — before any `wsync serve` ran there: `wsync status: no database in <dir>: wsync serve has not run there yet`, exit 1.
- `resume` — `wsync resume <name>`: `<name> resumed: claims start again (<way>)` when paused, `<name> is not paused (<way>)` when active, exit 0; an unknown name: `wsync resume: nothing is named '<name>' (<way>); see wsync status`, exit 1. Logged as `"pause cleared"` with `"by": "cli"`.
- `admin-status` — `GET /admin/status` on `[service] admin_listen`: the same data as JSON (`sinks`, `jobs`, `last_flush`); 503 while starting or stopping.
- `admin-resume` — `POST /admin/resume/{name}`: `{"name": ..., "result": "resumed" | "active"}`, 404 with `"unknown"`; the `x-wsync-by` header names who asked.

## How to get to it (user POV)
- `wsync status [--config webhook-sync.toml]`
- `wsync resume <sheets|razorpay> [--config webhook-sync.toml]`
- `GET http://<admin_listen>/admin/status`, `POST http://<admin_listen>/admin/resume/{name}` (loopback only).

## Driving it with wsync and curl
Preconditions: launched and Doctor `OK`; for a pause to clear, first drive sheets-mirror.md's "Pause on a refused key".
- **Status through the server.** Run `wsync status --config .verify/run/webhook-sync.toml`. Exit 0; lines `server: through the running server`, `razorpay (source): active`, `sheets (sink): ...`, `jobs: ledger N waiting, N dead; mirror N waiting, N dead`, `last flush: ...`.
- **Admin status.** Run `curl -s -w ' %{http_code}\n' http://127.0.0.1:$(cat .verify/run/admin_port)/admin/status`. JSON with the same sinks and counts, then `200`.
- **Resume.** Run `wsync resume sheets --config .verify/run/webhook-sync.toml`. `sheets resumed: claims start again (through the running server)`, exit 0; `grep '"pause cleared"' .verify/run/serve.log` shows `"target": "sheets", "by": "cli"`.
- **Resume, nothing paused.** Run `wsync resume razorpay --config .verify/run/webhook-sync.toml`. `razorpay is not paused (through the running server)`, exit 0.
- **Unknown name.** Run `wsync resume nosuch --config .verify/run/webhook-sync.toml`. stderr `wsync resume: nothing is named 'nosuch' (through the running server); see wsync status`, exit 1.
- **Admin resume.** Run `curl -s -X POST -H 'x-wsync-by: verifier' http://127.0.0.1:$(cat .verify/run/admin_port)/admin/resume/sheets`. `{"name":"sheets","result":"resumed"}` (or `"active"`); the log line has `"by": "verifier"`.
- **No database.** Write the scratch config from serve-start.md's "Missing secret" to `.verify/scratch/webhook-sync.toml` and run `wsync status --config .verify/scratch/webhook-sync.toml`. stderr `wsync status: no database in ...\.verify\scratch\data: wsync serve has not run there yet`, exit 1. Remove `.verify/scratch` after.

## Gotchas
- `status-on-database` can't be driven with the helper: Cleanup stops the server and deletes `.verify/run` in one step. `tests/test_cli_resume_status.py` proves it.
- The sheets pause comes back within about 3 s after a resume offline (the fake refuses the key); read the resume's own output and log line as the proof.
- Both commands read only `[service] data_dir` and `admin_listen` from the config; they need no secrets.
