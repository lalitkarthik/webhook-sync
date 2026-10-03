# Ledger rows

For every handled event the service fetches the payment's current state from Razorpay and
keeps exactly one business row per payment in the ledger: status, amount, refunds, fee, GST on
the fee, net, method, email and metadata, updated in place by later events.

## Sub-features
- `ledger-fetch` — a handled event queues one ledger job; the job calls `GET /v1/payments/{id}?expand[]=refunds&expand[]=card` and writes the row (fetch mode: the event's own contents are never written).
- `ledger-one-row` — later events for the same payment update the same row; `row_version` counts real changes.
- `ledger-statuses` — Razorpay `captured` → `succeeded`, `failed` → `failed`, partly refunded → `partially_refunded` with `refunded_minor` from processed refunds.
- `ledger-money` — minor units: `amount_minor`, `fee_minor`, `fee_tax_minor`, `net_minor` = amount − fee (before refunds); fee and net null while pending or failed.
- `ledger-created-no-row` — a payment still `created` at the source makes no row.
- `ledger-not-found` — a fetch answered "The id provided does not exist" marks `not_found_at` and writes nothing else.
- `ledger-retry` — a fetch that cannot connect or gets a 5xx is retried with backoff; after its attempts the job is dead.

## How to get to it (user POV)
- Razorpay delivers `payment.authorized`, `payment.captured`, `payment.failed`, `refund.created`, `refund.processed`, `refund.failed` or `refund.speed_changed` to `POST /webhooks/razorpay`.
- The buyer reads the row (today through the mirror or the database; `wsync verify` is not built).

## Driving it with the helper
Preconditions: launched and Doctor `OK`; Launch's fake Razorpay answers the recorded fetches for `pay_Tj9rKBOXWcsMeE`, `pay_Tj9sm13R8h2378`, `pay_Tj9gmEfed6kRqO` (and the other ids in `tests/recordings/razorpay/fetch/`); any other id gets the recorded 400.
- **Captured payment.** Run `<helper> deliver Tj9sMcl7hE9PaT`, then `<helper> sql --wait 15 "SELECT payment_id, status, amount_minor, fee_minor, fee_tax_minor, net_minor, method, via, last_event_id FROM payments"`. One row: `pay_Tj9rKBOXWcsMeE`, `succeeded`, 50000, 1180, 180, 48820, `netbanking (CNRB)`, `delivery`, `Tj9sMcl7hE9PaT`.
- **The fetch happened.** Run `cat .verify/run/upstreams.log`. `"GET /v1/payments/pay_Tj9rKBOXWcsMeE?expand%5B%5D=refunds&expand%5B%5D=card HTTP/1.1" 200`.
- **Job done.** Run `<helper> sql "SELECT stage, sink, status, attempts FROM jobs WHERE stage = 'ledger'"`. `ledger`/`ledger`/`done`/1.
- **Failed payment.** Run `<helper> deliver Tj9sobwBnTgcRB`, then `<helper> sql --wait 15 "SELECT status, fee_minor, net_minor FROM payments WHERE payment_id = 'pay_Tj9sm13R8h2378'"`. `failed`, null, null.
- **Partly refunded.** Run `<helper> deliver Tj9tnpcnWxNGXY` (refund.processed), then `<helper> sql --wait 15 "SELECT status, amount_minor, refunded_minor, net_minor FROM payments WHERE payment_id = 'pay_Tj9gmEfed6kRqO'"`. `partially_refunded`, 50000, 20000, 48820.
- **Still one row.** After any repeat of a delivery for the same payment, `<helper> sql "SELECT payment_id, count(*) AS n FROM payments GROUP BY payment_id"` shows `n` 1 for each.

## Gotchas
- The row comes from the fetch, not the event: the recorded fetch decides the values, whatever the delivered body says.
- The job runs about a second after the 202; wait with `sql --wait`, never a fixed sleep.
- `created`-state payments and not-found ids can't be reached with the recorded deliveries (each names a payment the fake answers); those paths are proved by the in-process tests.
- Emails in the recordings are the placeholder `customer@example.com`; masking (`mask_email`) applies to the sheet, not the ledger.
