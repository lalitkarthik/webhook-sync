# Webhook sync

Receives payment events from providers such as Stripe and Razorpay, keeps exactly one record of each, and keeps a database and a Google Sheet in step with them, proving nothing was lost or written twice.

## Language

**Source**:
A system that sends webhooks to us: Stripe, Razorpay, or later any sender following the Standard Webhooks spec.
_Avoid_: provider (except when quoting provider docs), integration, origin

**Source adapter**:
The code for one source: how its signature is checked, where its event id lives, how its events map to business rows, and how missed events are fetched back.
_Avoid_: connector, plugin, integration

**Sink**:
A place we write business rows to: the ledger, or a mirror such as a Google Sheet.
_Avoid_: destination, target, output

**Sink adapter**:
The code for one sink: how rows are written, in what batches, and what counts as a retryable failure.
_Avoid_: writer, connector

**Ledger**:
The database tables that are the single source of truth. The ledger enforces one row per payment with a unique key; every mirror is rebuilt from it.
_Avoid_: master, main DB, store

**Mirror**:
A copy of the ledger in a place people look at, such as a Google Sheet. A mirror is never read back as truth and can always be rebuilt.
_Avoid_: projection (in conversation), replica, export

## Events

**Event**:
One message from a source saying something happened to one object: "payment succeeded", "refund created". Every event is stored once, as received, and kept as the audit trail.
_Avoid_: webhook (the delivery, not the content), message, notification

**Delivery**:
One HTTP request carrying an event. A source may deliver the same event several times; each delivery is logged, but the event is stored once.
_Avoid_: request (in design talk), hit, call

**Event id**:
The source's own identifier for an event. Together with the source it is the **dedupe key**: `(source, event_id)`.
_Avoid_: message id, idempotency key (that is a different, client-sent header)

**Duplicate**:
A delivery whose dedupe key is already stored with the same body. It is answered 200 and changes nothing.

**Conflict**:
A delivery whose dedupe key is already stored with a different body. The first body is kept, the delivery is answered 200, and the conflict is logged for a person to look at.
_Avoid_: collision, mismatch

**Rejection**:
A delivery refused before storing: bad signature, stale or future timestamp, wrong mode, malformed body, too large. Logged without secrets.
_Avoid_: error, failure (those are for work after storing)

**Ignored event**:
A verified event of a type we don't handle. Stored and answered 200, but it creates no work.
_Avoid_: skipped, unsupported

**Mode**:
Test or live. Each source is configured with one mode; an event of the other mode is a rejection.
_Avoid_: environment, sandbox (Stripe's own word, used only when quoting Stripe)

## Business rows

**Payment**:
One charge a customer made to the buyer, as the source sees it now: amount, currency, status, how much is refunded, whether it is disputed. The ledger has exactly one business row per payment.
_Avoid_: transaction, charge (Stripe's object name, used only when quoting Stripe), order

**Business row**:
A row in the ledger's `payments` table, and the matching row in a mirror. Its key is the source plus the source's payment id.
_Avoid_: record, entry

**Fetch mode**:
Treating an event as a hint that one object changed, fetching that object's current state from the source's API, and writing that. The default.
_Avoid_: pull mode, refresh

**Payload mode**:
Writing the state carried inside the event itself, guarded by the event's `(created, event_id)`. Used only when a fetch fails permanently; rows written this way are flagged.
_Avoid_: push mode, event mode

**Terminal state**:
A payment status nothing can move it out of. Only `canceled` is terminal: a failed payment can still succeed, and a refunded one goes back to partially refunded if a refund fails. In payload mode, an event that would move a row backwards is ignored for that row.
_Avoid_: final state, end state

**Minor units**:
The integer form sources use for money: 1999 is 19.99 in USD or INR, but 1999 in JPY. The ledger stores minor units plus the currency; mirrors show major units.
_Avoid_: cents, paise (currency-specific)

## Work after storing

**Job**:
One piece of work still to do, kept as a row next to the event that caused it.
_Avoid_: task, message, work item

**Outbox**:
The rule that a change and the job it causes are written in one transaction, so neither can exist without the other.
_Avoid_: transactional queue

**Ledger job**:
The job that fetches one payment from its source and writes its business row to the ledger.
_Avoid_: apply job, sync job

**Mirror job**:
The job that copies one payment's current business row to one mirror.
_Avoid_: sheet job, export job

**Coalescing**:
Folding new work for a payment into the job already waiting for it, so there is at most one waiting job per stage, sink and payment.
_Avoid_: merging, deduping (dedupe is for deliveries)

**Claim**:
A worker taking a waiting job to run it. A claim gives the job a **lease** (an expiry time) and a **claim token**.

**Heartbeat**:
A running worker pushing its lease forward while it is still working.

**Reaper**:
The loop that returns jobs whose lease has expired to waiting.

**Fencing**:
The rule that a job can only be completed or failed by the worker whose claim token it still carries.

**Attempt**:
One run of a job, successful or not. Every attempt is logged.

**Dead**:
A job that has used all its attempts, or failed in a way retrying can't fix. Dead jobs are kept with their history until retried or resolved.
_Avoid_: failed (a failed attempt may still be retried), poisoned

**Dead-letter queue**:
All dead jobs, as shown on the admin page with retry and resolve actions.
_Avoid_: DLQ in prose (fine in code)

**Paused sink**:
A sink that has stopped taking jobs because every job would fail: repeated failures, a revoked key, a deleted sheet, a changed layout. A **probe** tests it every minute.
_Avoid_: disabled, broken, tripped

**Flush**:
One batched write of waiting mirror jobs to the sheet.
_Avoid_: sync, push

**Rebuild**:
Writing a fresh copy of the whole mirror from the ledger into a new tab and swapping it in.
_Avoid_: resync, reset, refresh

## Catching up and proving it

**Sweeper**:
The loop that asks a source for everything since the checkpoint and stores whatever is missing, through the same path as a delivery.
_Avoid_: poller, crawler, backfiller (a **backfill** is a sweep over a range a person chose)

**Checkpoint**:
The point in a source's history up to which we know we have everything. Saved in the same transaction as the events a sweep stores.
_Avoid_: cursor, watermark, offset

**Horizon**:
How far back a source lets us list events: 30 days for Stripe. Past it, only a full backfill by creation date can catch up.

**Origin**:
How an event reached us: `delivery`, `sweep`, `backfill` or `replay`. Recorded on every event.
_Avoid_: channel, source (source means the sender)

**Silent webhooks**:
The state where a source has stopped delivering to us, detected when the sweeper keeps finding events that never arrived as deliveries.

**Verify**:
The check that the source, the ledger and the sheet agree. Each payment that disagrees is **missing** (at the source, not in the ledger), **stale** (in both, but different) or **orphaned** (in the ledger, not at the source).
_Avoid_: audit, reconcile (reconcile is demo 1's word for balance checks)

**Repair**:
Fixing what verify found by enqueuing ordinary jobs, never by editing rows directly.

**Admin action**:
Anything a person does from the admin page or CLI (replay, backfill, rebuild, retry, resolve, resume), logged with who and when.

## Testing

**Fake**:
A stand-in we write for an outside service (FakeStripe, FakeSheets) that behaves like it, including its failures, so tests run offline.
_Avoid_: mock (a mock checks calls; a fake behaves), stub

**Invariant**:
A statement that must hold once everything settles, whatever happened on the way: the ledger equals the source, the sheet equals the ledger, every key appears once, no job is stuck.
_Avoid_: assertion, property (Hypothesis's word for the test itself)

**Crash point**:
A named place in the code where a test can kill the worker mid-flight, to prove that restarting loses nothing and duplicates nothing.
_Avoid_: fault injection point, kill switch

**Scene**:
One scripted demonstration for the video, driven by `wsync demo <scene>`.

## People

**Buyer**:
The business the service runs for: a founder or small business taking payments.
_Avoid_: customer (reserved for the buyer's own paying customers), user, client (reserved for freelance work)
