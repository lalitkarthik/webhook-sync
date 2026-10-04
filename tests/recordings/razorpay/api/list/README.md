# Razorpay list answers (build step P4)

Recorded on **2026-10-03, 07:52-07:56 UTC**, from a Claude Code cloud session, for the sweeper and the hourly re-list. The research note that reads them is [razorpay-list-facts.md](../../../../../docs/research/razorpay-list-facts.md). Every call went to `https://api.razorpay.com` in **test mode**. The helper refused any key id that didn't start `rzp_test_`, and it never printed or saved the key. **GET only**: nothing was created, captured or refunded. The account held the same 7 payments and 2 refunds as [../../fetch/](../../fetch/README.md).

## Shape

Each file is a JSON array of answers (or one answer). Each answer has `recorded_at`, `request` (method, path and query), `http` (status), `elapsed_ms` (measured from the cloud session, through its proxy), `headers` (only `content-type`, `date` and `uber-trace-id`, when present), and then one of:
- `body`: the parsed JSON, for errors, single fetches and the five full lists below;
- `body_summary`: for the other lists. It holds the collection's `entity`, `count` and top-level keys, and for each item its `id`, `created_at`, `status` (and `payment_id` for a refund), plus any expanded key (`refunds`, `card`, `emi`, `emi_plan`, `offers`, `token`, `transaction`) exactly as returned.

## Files

| File | What |
|---|---|
| `payments-list-count100.json` | `GET /v1/payments?count=100`, full body: the 7 payments, newest first |
| `payments-list-expand-all.json` | The same list with `expand[]` refunds, card, emi, offers, token and transaction, full body. **Every `refunds` is an empty collection**, even on `pay_Tj9gmEfed6kRqO`, which has two processed refunds. No `transaction` key at all |
| `payment-fetch-expanded-pay_Tj9gmEfed6kRqO.json` | The single fetch with `expand[]=refunds&expand[]=card`, for comparison: `refunds.count` 2 |
| `payments-paging.json` | `count` and `skip`, including skip past the end |
| `payments-bounds.json` | `from`/`to` equal to a payment's exact `created_at`, one-sided bounds, `from > to`, milliseconds, negative, a decimal, an empty `from`, no parameters |
| `payments-invalid-count-skip.json` | `count` 0, 101, -1, `abc`; `skip` -1, `abc` |
| `payments-expand.json` | Each `expand[]` on the list, one at a time and combined; `expand[]=bogus`; `expand=refunds` without brackets (a **500**) |
| `payments-cursor-and-sort-params.json` | `after`, `before`, `starting_after`, `ending_before`, `cursor`, `sort`, `order`: all rejected |
| `refunds-list-count100.json` | `GET /v1/refunds?count=100`, full body: the 2 refunds |
| `refund-fetch-rfnd_Tj9tMhKlOulF3Q.json` | `GET /v1/refunds/{id}`, for comparison with a listed refund (same keys) |
| `refunds-paging.json`, `refunds-bounds.json`, `refunds-invalid-count-skip.json` | The payment-list questions, asked of `/v1/refunds` |
| `refunds-filters.json` | `payment_id` (filters), `status` (accepted but **does not filter**), `expand[]=payment` (ignored), `starting_after` and `after` (ignored) |
| `headers-timing.json` | Full response headers of one payments list and one refunds list; times of 10 + 5 sequential lists and 20 lists over 10 threads (all 200) |

**Redacted in every file**, as in [../../fetch/](../../fetch/README.md): `email` is `customer@example.com`; `contact` and `vpa` are removed; card `last4` and `name` would be removed (no card payment exists); acquirer references read `<redacted>`; and here **every value in `notes` reads `<redacted>`**, with its keys kept. Everything else is as Razorpay sent it.

## Redaction check

Run from the repo root. Each command must print `0`, apart from the email one, which must print only `customer@example.com`. The key commands read the values from the environment and never print them.

```bash
D=tests/recordings/razorpay/api/list
grep -rlF "$RAZORPAY_KEY_ID" "$D" | wc -l
grep -rlF "$RAZORPAY_KEY_SECRET" "$D" | wc -l
grep -rnE 'rzp_(live|test)_[A-Za-z0-9]' "$D" | wc -l
grep -rhoE '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]+' "$D" | sort -u
grep -rnE '(\+91|\b0?)[6-9][0-9]{9}\b' "$D" | wc -l
grep -rnE '"(contact|vpa|last4|phone)"' "$D" | wc -l
grep -rn '"name"' "$D" | wc -l
```

On 2026-10-03 every count was 0 and the only email was `customer@example.com`.
