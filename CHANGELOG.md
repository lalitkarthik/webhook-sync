# Changelog

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
