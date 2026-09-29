# Showcase

**What this page contains.** What the demo has to show a buyer, the five scenes of the 60-second video, the `wsync demo` command that drives them, and the README's outline.

**How to read it.** The scenes table is the video script. Each scene maps to a test in [testing.md](testing.md), so what the video shows is also what CI proves.

## What a buyer must come away believing

The posted budgets for "connect Stripe to my sheet" run $10-250, below the $250 floor. The demo earns a $300-500 price only by showing what no-code tools can't: no duplicates, no silent loss, catching up after an outage, and a nightly proof. "It connects X to Y" is worth $50; "it can't lose or double-count a payment, and here's the proof" is the product (vault research, [Q1](grilling-log.md)).

## The five scenes

Result first: Upwork previews only the first 5-10 seconds on hover ([Q56](grilling-log.md)). The screen shows the sheet on the left and the admin Overview on the right.

| # | Scene | Seconds | What the viewer sees | Proved in CI by |
|---|---|---|---|---|
| 1 | **Duplicate storm** | 0-12 | One payment; `stripe events resend` five times. The sheet keeps one row; the duplicates counter goes 0 → 4. | load: duplicate storm |
| 2 | **Outage and catch-up** | 12-26 | Server stopped; 10 payments triggered; server started. The sheet fills to 10/10, each row tagged `sweep`. | crash points, sweeper integration |
| 3 | **Sheet access revoked** | 26-40 | Service account unshared: backoff visible on the timeline, then the red "paused: 403" banner. Re-shared: Resume. All rows arrive; nothing lost. | property: FakeSheets 403 |
| 4 | **Burst** | 40-52 | 200 payments at once. The API-call counter stops at about 6; 200 rows. Caption: "n8n forum: 56 of 100". | load: burst |
| 5 | **End card** | 52-60 | Verify page: "Verified nightly: 0 differences". Name and contact. | verify integration |

Title and end cards are made once with HyperFrames (Apache-2.0); recording with OpenScreen, captions from local Whisper, edit in Clipchamp (vault demo-video research). A separate GitHub README GIF per scene stays under 10 MB.

## wsync demo

Each scene is a command, so a retake costs a minute instead of a setup ([Q56](grilling-log.md)):

| Command | Does |
|---|---|
| `wsync demo reset` | Empties the database and the test spreadsheet's Payments tab; sets `RETRY_PROFILE=demo` |
| `wsync demo duplicates` | Creates one payment through the Stripe CLI, waits for its row, resends its event five times |
| `wsync demo outage` | Stops the receiver, triggers 10 payments, waits, restarts, runs a sweep |
| `wsync demo revoke` | Prints the steps to unshare and re-share (the service account can't unshare itself), then watches until paused and resumed |
| `wsync demo burst` | Triggers 200 payments and shows the Sheets call counter |

`demo` refuses to run unless every source is in test mode.

## The README

In this order ([Q57](grilling-log.md)):

1. **The problem**, in two lines, with Zapier's own help page on duplicate rows and n8n's 56-of-100 thread.
2. **Four GIFs**, one per scene.
3. **The guarantees**, each with the test that proves it:
   - one row per payment, however often an event is delivered
   - nothing acknowledged is ever lost, even through a crash
   - events missed while down are recovered automatically
   - a broken sheet or key pauses and resumes; nothing is dropped
   - every night, source, database and sheet are proved to agree
4. **Architecture**: the overview diagram from [00-overview.md](00-overview.md).
5. **Quickstart** in five commands: clone, `uv sync`, copy `.env.example`, `wsync doctor`, `wsync serve` with `stripe listen`.
6. **Configuration**: the TOML, briefly.
7. **Honest limits**: Razorpay is polled, so its event trail has gaps; one buyer per deployment; rows appear a few seconds after the event; a sheet exported to CSV can still carry formula text into Excel.
8. **Credits**: ported code and licences ([ADR 0005](decisions/0005-permissive-licences-only.md)).

## Open questions

- Whether to also record a 2-3 minute walkthrough for buyers who want more than 60 seconds. Written: later, if a pitch asks.

## Where to go next

[build-order.md](build-order.md): the order it gets built in, and when it is ready to pitch.
