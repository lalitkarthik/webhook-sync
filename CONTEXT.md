# Webhook sync

Receives payment events from providers such as Razorpay (first) and Stripe (parked), keeps exactly one record of each, and keeps a database and a Google Sheet in step with them, proving nothing was lost or written twice.

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
A delivery whose dedupe key is already stored with the same fingerprint. It is answered 200 and changes nothing. Sources may resend one event with different bytes; that is still a duplicate.

**Fingerprint**:
What an event means, as opposed to its bytes: its type, object id, creation time and mode.
_Avoid_: body hash, checksum

**Conflict**:
A delivery whose dedupe key is already stored with a different fingerprint. The first event is kept, the delivery is answered 200, and the conflict is logged for a person to look at.
_Avoid_: collision, mismatch

**Rejection**:
A delivery refused before storing: bad signature, stale or future timestamp, wrong mode, malformed body, too large. Logged without secrets.
_Avoid_: error, failure (those are for work after storing)

**Ignored event**:
A verified event of a type we don't handle. Stored and answered 202, like any new event, but it creates no work.
_Avoid_: skipped, unsupported

**Mode**:
Test or live. Each source is configured with one mode; an event of the other mode is a rejection.
_Avoid_: environment, sandbox (Stripe's own word, used only when quoting Stripe)

## Business rows

**Payment**:
One charge a customer made to the buyer, as the source sees it now: amount, currency, status, how much is refunded, whether it is disputed. The ledger has exactly one business row per payment.
_Avoid_: transaction, charge (Stripe's object name, used only when quoting Stripe), order

**Business row**:
A row in the ledger's `payments` table, and the matching row in a mirror. Its key is the source, the account at that source, and the source's payment id.
_Avoid_: record, entry

**Account**:
One account at a source (one Stripe account, one Razorpay account). Version 1 has one per source; the key carries it so a second never needs a migration.
_Avoid_: tenant, merchant

**Fetch mode**:
Treating an event as a hint that one object changed, fetching that object's current state from the source's API, and writing that. The only mode: the event's own contents are never written.
_Avoid_: pull mode, refresh

**Not found at source**:
A payment whose fetch returns 404, usually deleted test data. The row is marked, verify lists it, nothing else is written.
_Avoid_: orphan (verify's word for the same row seen from the other side)

**Fee** / **Net**:
What the source kept for processing a payment, and what the buyer receives: net = amount − fee. Both in minor units, in the settlement currency. **Pending** until the source has created its balance transaction; Stripe card fees arrive with `charge.updated`, usually within an hour.

**Terminal state**:
A payment status nothing can move it out of. Only `canceled` is terminal: a failed payment can still succeed, and a refunded one goes back to partially refunded if a refund fails.
_Avoid_: final state, end state

**Settlement currency**:
The currency the buyer is paid out in, which can differ from the currency the customer paid in.
_Avoid_: payout currency, reporting currency (a reporting currency would be a conversion we don't do)

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

**Writer**:
The one database connection every write goes through, one transaction at a time.
_Avoid_: write connection pool, DB lock

**Ledger job**:
The job that fetches one payment from its source and writes its business row to the ledger.
_Avoid_: apply job, sync job

**Mirror job**:
The job that copies one payment's current business row to one mirror.
_Avoid_: sheet job, export job

**Coalescing**:
Folding new work for a payment into its one live job, so there is never more than one job per stage, sink and payment that is waiting or running. New work for a running job sets its **rerun** flag instead.
_Avoid_: merging, deduping (dedupe is for deliveries)

**Rerun**:
The flag on a running job that says "something new arrived; run once more when you finish". On completion, a job with rerun set goes straight back to waiting.
_Avoid_: follow-up job, requeue

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

**Paused sink** / **paused source**:
A sink or source that has stopped taking jobs because every job would fail: repeated failures, a revoked key, a deleted sheet, a changed layout. Each pause has a reason, and a **probe** for that reason tests it, backing off to every 15 minutes; some reasons wait for a person to resume.
_Avoid_: disabled, broken, tripped

**Flush**:
One batched write of waiting mirror jobs to the sheet.
_Avoid_: sync, push

**Rebuild**:
Rewriting a mirror's tabs in place from the ledger while mirror writes are paused, then verifying them before resuming.
_Avoid_: resync, reset, refresh

**Machine-owned tab**:
A tab only the service writes: the Payments and Daily summary tabs. Buyers read, filter and sort them through filter views, and put their own work in their own tabs.
_Avoid_: locked tab, protected tab

**Daily summary**:
The tab with one row per day, per currency paid in and per currency paid out: count, gross and refunds in the first; settled, fees and net in the second; from payments that succeeded.
_Avoid_: report, dashboard

**Forwarding sink**:
A sink that sends each business-row change, once and signed, to the buyer's own automation (n8n, Zapier, Make).
_Avoid_: relay, proxy, webhook forwarder

**Database sink**:
A sink that writes business rows, and later subscriptions, into the buyer's own application database.
_Avoid_: Postgres sink (the database may be Supabase or another), export

## Catching up and proving it

**Sweeper**:
The loop that asks a source for everything since the checkpoint and stores whatever is missing, through the same path as a delivery.
_Avoid_: poller, crawler, backfiller (a **backfill** is a sweep over a range a person chose)

**Checkpoint**:
The point in a source's history, by the source's own clock, up to which we know we have everything. It moves forward one **time slice** at a time, only after the whole slice has been read and stored.
_Avoid_: cursor, watermark, offset

**Time slice**:
One window of source time (for example ten minutes) that a sweep reads completely before moving the checkpoint past it. Each sweep re-reads a 10-minute overlap before the checkpoint.
_Avoid_: page (a page is one API response), batch

**Horizon**:
How far back a source lets us list events: 30 days for Stripe. Past it, only a full backfill by creation date can catch up. Razorpay has no events list, so it has no horizon: its sweeper lists payments by creation date.

**Re-list**:
The sweeper's hourly second read of a source with no events list (Razorpay): every payment created in the last 7 days and every refund created in the last 21, so a change to an older payment reaches its row.
_Avoid_: re-fetch (one payment by id), poll (too vague)

**Synthetic event**:
An event the sweeper builds from a listed payment or refund when the source has no events list (Razorpay), with origin `sweep` and the id `sweep:<entity id>:<state hash>`. It triggers a fetch like any other event.
_Avoid_: fake event (a **fake** is a test stand-in)

**State hash**:
A hash of the fields of a listed payment or refund that can change its row. A new value becomes a **synthetic event** `sweep:<entity id>:<state hash>`; the same value is already stored.
_Avoid_: fingerprint (that identifies a delivered event)

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
Anything a person does from the admin port or the CLI (replay, backfill, rebuild, retry, resolve, resume), logged with which token and when.

**Heartbeat URL**:
An outside service the sweeper pings after every sweep; when the pings stop, it tells a person the service is down. Also called a dead-man's switch.
_Avoid_: health check (that is the `/health` endpoint an outside monitor calls)

## Testing

**Fake**:
A stand-in we write for an outside service (FakeRazorpay, FakeSheets, later FakeStripe) that behaves like it, including its failures, so tests run offline.
_Avoid_: mock (a mock checks calls; a fake behaves), stub

**Invariant**:
A statement that must hold once everything settles, whatever happened on the way: the ledger equals the source, the sheet equals the ledger, every key appears once, no job is stuck.
_Avoid_: assertion, property (Hypothesis's word for the test itself)

**Crash point**:
A named place in the code where a test can kill the worker mid-flight, to prove that restarting loses nothing and duplicates nothing.
_Avoid_: fault injection point, kill switch

**Scene**:
One scripted demonstration for the video, driven by `wsync demo <scene>`.

**Demo fail-first switch**:
`wsync serve --demo-fail-first N`: stores each new event as usual, then answers 500 to its first N attempts, so the source's own retries make the duplicate-storm scene. Test mode and the demo allow-list only.
_Avoid_: chaos mode, fault injection (crash points are the tests' fault injection)

## People

**Buyer**:
The business the service runs for: a founder or small business taking payments, a SaaS founder whose app needs payment state, or a team whose n8n or Zapier automations need clean events.
_Avoid_: customer (reserved for the buyer's own paying customers), user, client (reserved for freelance work; in handover and quote text, the freelance client *is* the buyer)

**Restricted key**:
A Stripe API key limited to named permissions; the service requires one with read access only.
_Avoid_: API key (too broad), secret key (Stripe's name for a full-access key)
