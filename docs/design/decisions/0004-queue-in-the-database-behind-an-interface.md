# The job queue lives in the database, behind an interface

Status: accepted (2026-09-30)

Jobs are rows in a `jobs` table in the same database as the events. Storing an event and creating its job happen in one transaction, so the service can never answer 202 and then lose the work. There is no Redis in version 1. The queue is reached only through six operations (`enqueue`, `claim`, `heartbeat`, `complete`, `fail`, `reap`), so another backend can replace the table later.

Redis Streams was the natural alternative: the author knows it from the Delta Exchange feed engine. It was turned down for this product because:
- **The event has to be in the database anyway.** Only a unique constraint there guarantees no duplicates. A Redis queue makes receiving two writes to two systems, and a crash between them loses the job after the source has been told "got it". The standard fix, an outbox table relayed to Redis, is already a queue on its own.
- **Default Redis persistence can lose jobs on restart.** Losing work silently is exactly the failure this product is sold against.
- **Setup.** Redis has no official Windows build, so the zero-setup demo would need Docker, and each client would need a Redis host to pay for and watch.
- **Volume.** A small business gets tens of payments an hour; a database queue handles hundreds of jobs a second on SQLite.

Every Redis Streams idea maps across, so the design reads naturally to someone who knows Streams:

| Redis Streams | This queue |
|---|---|
| `XADD` | `INSERT INTO jobs` |
| `XREADGROUP` | claim: set `running`, `lease_until`, `claim_token` |
| pending entries list | rows with `status = 'running'` |
| `XAUTOCLAIM` with a minimum idle time | the reaper: `lease_until < now` goes back to waiting |
| `XACK` | complete, only if the `claim_token` still matches |
| delivery count, then a dead stream | `attempts ≥ max` sets `status = 'dead'` |

A client who already runs Redis can later get a Streams backend that the outbox table feeds, without any change to receivers, workers or sinks.
