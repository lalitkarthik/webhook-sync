# Fetch the current state; never write the event payload

Status: accepted (2026-09-30); amended the same day after the design audit (Q73): the payload fallback is removed.

When an event arrives, the worker does not write what the event says. It treats the event as a hint that one payment changed, fetches that payment's current state from the source's API, and writes that. Several events for one payment waiting in the queue collapse into one fetch, and only one fetch per payment runs at a time ([ADR 0004](0004-queue-in-the-database-behind-an-interface.md)).

Neither Stripe nor Razorpay promises to deliver events in order, and Stripe warns that events can share the same `created` second. Writing payloads needs a version guard, and the studied repos show how that goes wrong: sync-engine's database guard uses `>=` while its Sheets guard uses `>`, so on a tie the two stores disagree. Fetching the current state removes the ordering problem instead of guarding against it.

**Why there is no payload fallback.** The first version wrote the event's own contents when a fetch returned 404. The audit (F09) showed its rules contradicted each other, that it could overwrite a newer fetched state with an older event, and that a 404 almost always means deleted test data or the wrong account, where a person should look. Now a 404 keeps the last fetched values, marks the row "not found at source", and verify lists it.

The costs:
- **One API call per changed payment.** Stripe allows about 500 reads per payment with a floor of 10,000 a month; [operations.md](../operations.md) budgets them.
- **A dependency on the source's API being up.** While it is down, ledger jobs back off and retry; nothing is written from stale payloads.

A future source with no API (the generic Standard Webhooks sender) can only work from payloads. It will get its own design when it is built; this decision covers sources that have an API.
