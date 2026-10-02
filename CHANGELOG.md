# Changelog

## P1: receive Razorpay events exactly once, and queue their work — 2026-10-02

- `wsync serve` starts, refuses anything unsafe in one line naming the problem, runs one server per database and applies migrations itself.
- `POST /webhooks/razorpay` verifies each delivery and stores a genuine event exactly once, together with its ledger job, answering 202; repeats, replays and conflicts are counted, and every rejection gets its own reason.
- A database job queue with leases, retries with backoff, dead jobs, a reaper and crash recovery is ready for P2's ledger worker; stopping the server loses nothing.
- `--demo-fail-first N` answers 500 to each event's first N attempts, for the duplicate-storm scene, in test mode only.
