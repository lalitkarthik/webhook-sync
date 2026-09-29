# One server process per database

Status: accepted (2026-09-30), after the design audit (F11, F14).

`wsync serve` takes an exclusive operating-system lock on a lock file next to the database when it starts, and refuses to start if another process holds it. One process therefore runs the receiver, the ledger worker, the flusher, the sweeper, verify and the pruner. Command-line actions that change state (`replay`, `backfill`, `resume`, `dead retry`, `verify --repair`) send a request to the running server's admin port when the lock is held, and run on their own only when no server is running.

The first design allowed a second process by mistake and relied on leases in a `singletons` table to stop two flushers. The audit showed why that isn't enough: Google Sheets can't check a fencing token, so a flusher that stalls past its lease still writes its stale row positions over the other flusher's rows (F11). And SQLite with two writing processes needs `busy_timeout`, which sheds load as errors (F14).

Refusing the second process is simpler and stronger than racing it. The cost is that scaling out means running Postgres and designing multi-process claims, which [ADR 0004](0004-queue-in-the-database-behind-an-interface.md) already defers until a client needs it.
