# Feature map

How to reach and drive every feature of this app as a user. Read by the feature-loop verifier,
the auditors and the test author. One file per feature; keep implementation details out.

Format adapted from pstack (https://github.com/cursor/plugins/tree/main/pstack), MIT License,
Copyright (c) 2026 Lauren Tan.

## Baseline
- Start only through `.claude/skills/verify-webhook-sync/SKILL.md` (Launch). Never drive an instance this run did not start.
- Locators: roles and accessible names first, then data attributes; never coordinates.
  This app has no pages: its stable handles are routes (`POST /webhooks/razorpay`), `wsync`
  subcommands and flags, JSON keys in answers, table and column names, and log `message` values.
- A mutation is proved by a second, read-only view of the stored result.
- Do not report an entry point as verified through a different path.
- "The helper" below is `.venv/Scripts/python.exe .claude/skills/verify-webhook-sync/wsyncv.py`
  (`.venv/bin/python` off Windows); `wsync` is `.venv/Scripts/wsync.exe` (`.venv/bin/wsync`).
  Every instance is test mode with fixture secrets; Razorpay's API and Google's token endpoint
  are local fakes. Built so far: P1-P3. Sweeper, backfill and `wsync verify` (P4) are not built.

## Features
| Feature | File | Entry points |
| --- | --- | --- |
| Receive Razorpay deliveries | [receive-deliveries.md](receive-deliveries.md) | `POST /webhooks/razorpay`, `GET /health`, `wsync serve --demo-fail-first N` |
| Ledger rows | [ledger-rows.md](ledger-rows.md) | a delivery of a handled event; the `payments` table |
| Sheets mirror | [sheets-mirror.md](sheets-mirror.md) | mirror jobs after a ledger change; `sheets` pauses; `[alerts] url` |
| Status and resume | [status-and-resume.md](status-and-resume.md) | `wsync status`, `wsync resume <name>`, `GET /admin/status`, `POST /admin/resume/{name}` |
| Start and refuse | [serve-start.md](serve-start.md) | `wsync serve --config <toml>`, `wsync --help`, `wsync --version` |
