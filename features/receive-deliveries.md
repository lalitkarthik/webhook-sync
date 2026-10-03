# Receive Razorpay deliveries

Razorpay posts each event to the public listener; the service checks it, stores every event
once (however often it is delivered), answers so Razorpay stops retrying, and queues the work.

## Sub-features
- `deliver-new` — a verified, new event is stored and answered `202 {"result": "new"}`.
- `deliver-duplicate` — the same event again (Razorpay's retry) is answered `200 {"result": "duplicate"}` and changes nothing but a `deliveries` row.
- `deliver-conflict` — the same event id with a different meaning is answered `200 {"result": "conflict"}`; the first event is kept and an error line is logged.
- `deliver-ignored` — a verified event of a type not handled (e.g. `order.paid`) is stored with outcome `ignored` and reason `unhandled_type`, answered 202, and queues no job.
- `deliver-rejected` — refused before storing, nothing written to `events`: `400 bad_signature`, `400 malformed`, `400 missing_id`, `400 wrong_account`, `400 duplicate_header`, `415 not_json`, `413 too_large`, `429 rate_capped`; counted per minute in `rejection_counts`.
- `deliver-shutting-down` — during shutdown new deliveries get `503 {"error": "shutting_down"}`.
- `health` — `GET /health` answers `200 {"status":"ok"}`.
- `demo-fail-first` — `wsync serve --demo-fail-first N` stores each new event, then answers 500 to its first N attempts (test mode and a sheet on `[demo] spreadsheet_ids` only).

## How to get to it (user POV)
- Razorpay's webhook, pointed at `https://<host>/webhooks/razorpay` (`POST`, headers `x-razorpay-signature`, `x-razorpay-event-id`, `content-type: application/json`).
- An outside monitor calling `GET /health`.
- The owner starting the demo with `wsync serve --demo-fail-first N`.

## Driving it with the helper and curl
Preconditions: launched and Doctor all `OK`; a fresh `.verify/run` (Launch makes one).
- **New event.** Run `<helper> deliver Tj9sMcl7hE9PaT`. `"status": 202`, `"body": {"result": "new"}`.
- **Second view.** Run `<helper> sql "SELECT event_id, type, outcome, job_id FROM events"` and `<helper> sql "SELECT event_id, result, status_code FROM deliveries"`. One event `payment.captured`, outcome `queued`, a `job_id`; one delivery `new`/202.
- **Retry.** Run `<helper> deliver Tj9sMcl7hE9PaT --attempt 2`. `"status": 200`, `{"result": "duplicate"}`; `events` still has one row, `deliveries` two.
- **Conflict.** After the new event, run `<helper> deliver Tj9sobwBnTgcRB --header-event-id Tj9sMcl7hE9PaT` (another event's body under the stored id). `"status": 200`, `{"result": "conflict"}`; `grep '"delivery conflicts' .verify/run/serve.log` shows `"level": "error"`, `"message": "delivery conflicts with the stored event: first event kept"`; the `events` row for `Tj9sMcl7hE9PaT` is still `payment.captured`.
- **Ignored type.** Run `<helper> deliver Tj9sMvWj9V6X75`. 202 `new`; its `events` row has outcome `ignored`, `ignore_reason` `unhandled_type`, `job_id` null.
- **Bad signature.** Run `<helper> deliver Tj9sobwBnTgcRB --bad-signature`. `"status": 400`, `{"rejection": "bad_signature"}`; no `events` row for it; `grep '"delivery rejected"' .verify/run/serve.log` shows `"reason": "bad_signature"`.
- **Not JSON.** Run `curl -s -w ' %{http_code}\n' -X POST -H 'content-type: text/plain' --data 'x' http://127.0.0.1:$(cat .verify/port)/webhooks/razorpay`. `{"rejection":"not_json"} 415`.
- **Health.** Run `curl -s -w ' %{http_code}\n' http://127.0.0.1:$(cat .verify/port)/health`. `{"status":"ok"} 200`.
- **Demo switch.** Cleanup, then `<helper> launch --demo-fail-first 2`; run `<helper> deliver Tj9sMcl7hE9PaT` three times with `--attempt 1`, `2`, `3`. Statuses 500 (body `new`), 500 (`duplicate`), then 200 (`duplicate`); `events` has one row (stored on the first attempt), `deliveries` three.

## Gotchas
- The signature covers the raw bytes: re-serialising a body breaks it. The helper re-signs the recorded bytes with the fixture secret; a recording's own signature used the real secret and never verifies here.
- Recordings name account `acc_Tj5AnsI7BH2aLR`; Launch configures that account. Razorpay's documented samples in `tests/fixtures/razorpay/` name another account and are for in-process tests.
- `rejection_counts` is written once a minute and at shutdown, so a rejection shows there only after the minute closes; the log line is immediate.
- The rate cap (429) needs a burst no single drive produces; leave it to the tests.
