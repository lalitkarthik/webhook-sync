# One buyer per deployment

Status: accepted (2026-09-30)

A running copy of the service belongs to one buyer, with one account per source: one Stripe account, one Razorpay account, one spreadsheet. Serving several businesses means running several copies.

The alternative, one deployment for many buyers, needs a tenant column on every table, a tenant check on every query and every admin page, and per-tenant secrets. A single missed check leaks one business's payments to another. That is a large cost for a demo and for the fixed-price jobs it sells, where each client gets their own copy anyway.

Stripe Connect (a platform receiving events for many connected accounts, marked by an `account` field) is out of scope for the same reason. Adding either later means adding the tenant column everywhere, which is why this is recorded.
