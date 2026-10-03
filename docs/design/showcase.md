# Showcase

**What this page contains.** Who the demo is for, the prototype GIF, the scenes of the pitch-ready video, the `wsync demo` command, and the README.

**How to read it.** The scenes table is the video script. Each scene maps to a check in [testing.md](testing.md), so what the video shows is also what CI proves.

## Who it is for

Three audiences, one core ([Q66](grilling-log.md)):
- **A small business that wants a live sales sheet.** The clearest demo: anyone understands "five resends, one row".
- **n8n and Zapier users who lose or double-count events.** The duplicate-storm scene shows their pain; the forwarding sink is the offer that fixes it ([next-adapters.md](next-adapters.md)).
- **A SaaS founder who needs payment and subscription state in their own database.** Where the posted money is; the database sink is the first paid adapter.

"Payments to a Google Sheet" alone already sells for $0-35 a month (Coupler, Stripe Marketplace apps, Pabbly), so the demo leads with what those don't do: updates within seconds, no subscription, self-hosted and owned by the buyer, and a nightly proof (audit, competitors section).

## The prototype GIF

About 20 seconds, from the 10-12 h prototype, against Razorpay test mode on the owner's laptop through a tunnel ([Q93](grilling-log.md), [Q106](grilling-log.md), [Q107](grilling-log.md)). It keeps 8 October only if the recording spike is done by 4 October ([Q114](grilling-log.md)).
1. **Duplicate storm.** One test payment. Razorpay has no resend button, so the receiver runs with `--demo-fail-first 4`: it stores the event, then answers 500 to the first four attempts, and Razorpay's own retries make the five deliveries. The sheet keeps one row; the duplicates counter reads 4. Caption: "Our server answered 500 to the first four attempts on purpose; Razorpay retried the same event." If the spike shows retries take longer than about 30 minutes, the scene replays one recorded delivery instead, captioned "one real delivery, recorded, then re-sent four more times by us; Razorpay signs only the body, so each copy verifies". The caption is on screen, not only in the README.
2. **Outage and catch-up.** Server stopped; three payments made; server started. The startup sweep brings the rows back with Via = `sweep`; Razorpay's own retries of the missed deliveries arrive later and change nothing: each is stored as an event, its fetch finds the row already current, and no sheet write follows. Still one row each ([Q145](grilling-log.md)).

## The pitch-ready video

Result first: Upwork previews only the first 5-10 seconds. The sheet on the left, a terminal with `wsync status` on the right.

| # | Scene | Seconds | What the viewer sees | Proved by |
|---|---|---|---|---|
| 1 | **Duplicate storm** | 0-15 | As in the GIF, with fee, GST and net filled in | load: duplicate storm |
| 2 | **Outage and catch-up** | 15-35 | Ten payments while down; ten rows after, tagged `sweep` | crash point `mid_slice`; outage scenario |
| 3 | **Verify and the Daily summary** | 35-55 | `wsync verify`: "last 30 days, N payments, 0 differences"; the Daily summary tab with gross, fees and net per day | verify scenario |
| 4 | End card | 55-60 | Name and contact | |

Later, for the full video (rescripted after the audit, F36):
- **Access revoked:** unshare the sheet; "paused: 403" within seconds, jobs waiting; re-share; Resume; the rows drain.
- **Burst:** 200 payments created before the flusher starts, then "200 rows, N Sheets calls" with the real N.

Title and end cards once with HyperFrames (Apache-2.0); recording with OpenScreen; captions from local Whisper; editing in Clipchamp (vault video research). README GIFs under 10 MB.

## wsync demo

| Command | Does |
|---|---|
| `wsync demo reset` | Empties the database and the demo spreadsheet's tabs; sets the demo retry profile |
| `wsync demo duplicates` | Restarts the receiver with `--demo-fail-first 4`, pays one test order, and waits for its row and Razorpay's retries |
| `wsync demo outage` | Stops the receiver, pays test orders, waits, restarts, runs a sweep |
| `wsync demo verify` | Runs a full verify and opens the Daily summary |

**How a test payment is made** ([Q105](grilling-log.md), G45). `demo` creates an order with `POST /v1/orders` and pays it in headless Chromium through `checkout.js`: Netbanking, any bank, then **Success** on Razorpay's mock bank page. A payment is captured about 30 s after the browser starts, with no human and no feature flag. Amounts are ₹100 or more by netbanking, so fee and GST show non-zero values. Playwright is in the optional `wsync[demo]` extra, so the service never needs a browser. If the headless run fails (a captcha, a changed page), `demo` prints the payment URL for a person to pay. Payment Links are not used: test mode allows 30 per business.

`demo` refuses to run unless the source is in test mode (key `rzp_test_`) **and** the spreadsheet id is on the configured demo allow-list, so a mistyped id can never empty a real sheet (F36).

## The README

1. **The problem**, in two lines: Zapier's own help page on duplicate rows, and n8n's 56-of-100 thread.
2. **The GIFs.**
3. **The guarantees**, each with the test that proves it: one row per payment however often an event arrives; nothing acknowledged is lost, even through a crash; events missed while down come back; later changes reach the same row; every night, the last 30 days are proved to match Razorpay, and the sheet the database.
4. **"Why not Coupler, Zapier or Hookdeck?"**, answered in four lines: seconds, not hours; no subscription; self-hosted and owned; the proof.
5. **Architecture**: the overview diagram.
6. **Quickstart** in five commands.
7. **Honest limits:** rows appear a few seconds after the event; one buyer per deployment; Razorpay has no events list, so changes to a payment older than 7 days (or a refund older than 21) arrive with nightly verify; Razorpay has no resend button, so the duplicate scene provokes Razorpay's retries (or says it is a replay); test-mode fees are illustrative, and GST is 0 on domestic cards up to ₹2,000; disputes and failed refunds can't be produced in test mode, so they are tested on Razorpay's sample payloads (disputes are not in version 1); Stripe is designed, not built; the sheet is machine-owned (use filter views); a sheet exported to CSV can carry formula text into Excel.
8. **Next adapters:** forwarding to n8n or Zapier; your own database with subscriptions.
9. **Credits** for ported code ([ADR 0005](decisions/0005-permissive-licences-only.md)).

## Where to go next

[build-order.md](build-order.md): the order it gets built in.
