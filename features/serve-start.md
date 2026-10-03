# Start and refuse

`wsync serve` starts the service only when it is safe: it takes the database's lock (one
server per database), validates the config and secrets, refuses a newer schema, applies
migrations and reclaims jobs a crashed run left running. Any unsafe start prints one line
naming the key or variable (never a value) and exits 2.

## Sub-features
- `start-ok` — a valid config: both listeners up, `/health` 200, `"Application startup complete."` in the log.
- `start-lock` — a second `wsync serve` on the same data directory: `wsync serve: another wsync serve holds the lock file <dir>/webhook-sync.lock: one server per database`, exit 2.
- `start-secrets` — a missing secret: e.g. `wsync serve: RAZORPAY_KEY_ID is missing or empty`, exit 2; a key of the wrong mode is refused the same way.
- `start-config` — a missing file, bad TOML, an unknown key, a secret in the TOML, or a parked `[sources.stripe]`: one line naming it, exit 2.
- `start-demo-guard` — `--demo-fail-first N` without a test key or with the sheet not in `[demo] spreadsheet_ids`: refused, exit 2.
- `help-version` — `wsync --help` lists `serve`, `resume`, `status`; `wsync --version` prints `wsync 0.1.0`.

## How to get to it (user POV)
- `wsync serve [--config webhook-sync.toml] [--demo-fail-first N]`, secrets from the environment or a `.env` next to the config.
- `wsync --help`, `wsync --version`.

## Driving it with wsync
Preconditions: for `start-lock`, launched and Doctor `OK`. Scratch configs go under `.verify/` only, and must not sit next to the real `.env`.
- **Started.** Run `grep -c '"Application startup complete."' .verify/run/serve.log`. `1`.
- **Second server refused.** Run `timeout 30 wsync serve --config .verify/run/webhook-sync.toml; echo "exit $?"`. `wsync serve: another wsync serve holds the lock file ...\webhook-sync.lock: one server per database`, `exit 2`; Doctor still `OK`.
- **Missing secret.** Write `.verify/scratch/webhook-sync.toml` with `[service] public_listen = "127.0.0.1:1"`, `data_dir = "data"`, `[sources.razorpay] mode = "test"`, `account = "acc_Tj5AnsI7BH2aLR"`, `[sinks.sheets] spreadsheet_id = "x"`; run `env -u RAZORPAY_KEY_ID -u RAZORPAY_KEY_SECRET -u RAZORPAY_WEBHOOK_SECRET -u GOOGLE_SERVICE_ACCOUNT_B64 timeout 30 wsync serve --config .verify/scratch/webhook-sync.toml; echo "exit $?"`. `wsync serve: RAZORPAY_KEY_ID is missing or empty`, `exit 2`. Remove `.verify/scratch` after.
- **Help and version.** Run `wsync --help` and `wsync --version`. The first line `usage: wsync [-h] [--version] <command> ...` and the commands `serve`, `resume`, `status`; `wsync 0.1.0`.

## Gotchas
- The lock is taken before the config is read, so a second server is refused even with no secrets set.
- A refused start leaves `<data_dir>` created (it is made before the lock); that is expected.
- Always wrap a `wsync serve` you expect to be refused in `timeout 30`: if it is not refused, it would start a server this run cannot clean up by PID.
