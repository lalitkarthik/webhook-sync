# Fetch the current state; don't trust the event payload

Status: accepted (2026-09-30)

When an event arrives, the worker does not write what the event says. It treats the event as a hint that one object (a payment, a refund, a dispute) changed, fetches that object's current state from the source's API, and writes that. Several events for the same object waiting in the queue collapse into one fetch.

Neither Stripe nor Razorpay promises to deliver events in order, and Stripe warns that events can share the same `created` second. Writing payloads needs a version guard, and the studied repos show how that goes wrong: sync-engine's database guard uses `>=` while its Sheets guard uses `>`, so on a tie the two stores disagree. Fetching the current state removes the ordering problem instead of guarding against it.

The cost is one API call per changed object, and a dependency on the source's API being up. Two safety nets cover the gaps:
- **Payload mode can't move a row backwards.** In payload mode, an older event never overwrites a newer one, and nothing moves a row out of `canceled`. (Fetch mode needs no such rule: the source's current state is the truth, including a refund that failed and lowered the refunded amount again.)
- **Payload mode is the fallback.** When a fetch fails permanently (404, revoked key), the event payload is written with a `(created, event_id)` guard and the row is flagged `from_payload`.
