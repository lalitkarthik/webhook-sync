# The job queue lives in the database, behind an interface

Status: accepted (2026-09-30); amended the same day after the design audit (Q70, Q75, Q83): one live job per payment, SQLite only in version 1, one writer connection.

Jobs are rows in a `jobs` table in the same database as the events. Storing an event and creating its job happen in one transaction, so the service can never answer 202 and then lose the work. There is no Redis in version 1. The queue is reached only through six operations (`enqueue`, `claim`, `heartbeat`, `complete`, `fail`, `reap`), so another backend can replace the table later.

Redis Streams was the natural alternative: the author knows it from the Delta Exchange feed engine. It was turned down for this product because:
- **The event has to be in the database anyway.** Only a unique constraint there guarantees no duplicates. A Redis queue makes receiving two writes to two systems, and a crash between them loses the job after the source has been told "got it". The standard fix, an outbox table relayed to Redis, is already a queue on its own.
- **Default Redis persistence can lose jobs on restart.** Losing work silently is exactly the failure this product is sold against.
- **Setup.** Redis has no official Windows build, so the zero-setup demo would need Docker, and each client would need a Redis host to pay for and watch.
- **Volume.** A small business gets tens of payments an hour; a database queue handles hundreds of jobs a second on SQLite.

**One live job per payment.** At most one job per (stage, sink, payment) is waiting or running. New work for a running job sets its `rerun` flag, and completing a job with `rerun` set puts it straight back to waiting. The first version allowed a waiting job next to a running one; the audit (F01) reproduced that failing, reaping or releasing the running job then broke the unique index and stopped the reaper for every job, and (F04) that two fetches for one payment could run at once with the older answer landing last.

**SQLite only, one writer.** Version 1 runs on SQLite. All writes go through one connection behind an async lock that issues `BEGIN IMMEDIATE` itself; pages and verify read through a separate read-only connection. The audit measured a connection pool with `busy_timeout` shedding load as "database is locked" errors (F14). Postgres, and several workers claiming with `FOR UPDATE SKIP LOCKED`, are added when a client needs them, on Linux only (F16).

Every Redis Streams idea maps across, so the design reads naturally to someone who knows Streams:

| Redis Streams | This queue |
|---|---|
| `XADD` | `enqueue`: insert, or set `rerun` on the live job |
| `XREADGROUP` | `claim`: set `running`, `lease_until`, `claim_token` |
| pending entries list | rows with `status = 'running'` |
| `XAUTOCLAIM` with a minimum idle time | the reaper: `lease_until < now` goes back to waiting |
| `XACK` | `complete`, only if the `claim_token` still matches |
| delivery count, then a dead stream | `attempts ≥ max` sets `status = 'dead'` |
