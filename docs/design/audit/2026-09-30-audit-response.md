# Audit response

**What this page contains.** What was done about each of the 38 findings in the [design audit](2026-09-30-design-audit.md): the decision (with its question in the [grilling log](../grilling-log.md), where one was needed) and the page that now carries it.

**How to read it.** Findings that needed a choice were asked in rounds 7 and 8 (Q66-Q93); every recommendation was accepted on 2026-09-30. The rest were plain fixes, made directly. Nothing was rejected.

## Summary

| Severity | Count | Needed a decision | Fixed directly |
|---|---|---|---|
| Critical | 2 | 2 | 0 |
| High | 11 | 11 | 0 |
| Medium | 19 | 10 | 9 |
| Low | 6 | 2 | 4 |

## Every finding

| ID | Sev. | Finding | What changed | Where |
|---|---|---|---|---|
| F01 | Critical | Fail, reap and release collide with the coalescing index | One live job per payment with a rerun flag; the reaper goes row by row | [Q70](../grilling-log.md), [queue.md](../queue.md), [ADR 0004](../decisions/0004-queue-in-the-database-behind-an-interface.md) |
| F02 | High | Conflicts by raw-body hash fire on normal traffic | Conflict = different fingerprint; the sweeper never compares and writes no deliveries row | [Q71](../grilling-log.md), [receiver.md](../receiver.md), [sweeper.md](../sweeper.md) |
| F03 | High | The sweep's order and checkpoint lose events; quiet accounts stall | Time slices on the source clock, 10-minute overlap; empty slices advance | [Q72](../grilling-log.md), [sweeper.md](../sweeper.md) |
| F04 | High | Two fetches for one payment race | Fixed by F01: one fetch per payment at a time; invariant 5 | [queue.md](../queue.md), [testing.md](../testing.md) |
| F05 | High | The post-write check can't see a sort | Keys written only on append; a moved row is repaired and alerted; twice pauses; machine-owned tabs | [Q63](../grilling-log.md), [Q74](../grilling-log.md), [sheets-sink.md](../sheets-sink.md), [ADR 0006](../decisions/0006-machine-owned-tabs.md) |
| F06 | High | Rebuild detaches buyer columns; a crash leaves the wrong tab live | Rebuild in place; a crash leaves "rebuild incomplete" | [Q92](../grilling-log.md), [sheets-sink.md](../sheets-sink.md) |
| F07 | Medium | Where new rows go | On a machine-owned tab the last key row is the last used row; grid extended only when needed | [sheets-sink.md](../sheets-sink.md) |
| F08 | Medium | Reads by title; 400s misclassed; probes blind to the reason | `getByDataFilter` by sheet id; 400s naming a sheet or range pause; one probe per reason | [sheets-sink.md](../sheets-sink.md), [queue.md](../queue.md) |
| F09 | Medium | Payload mode contradicts itself | Cut; a 404 marks the row "not found at source" | [Q73](../grilling-log.md), [ADR 0002](../decisions/0002-fetch-current-state-not-event-payload.md), [ledger-worker.md](../ledger-worker.md) |
| F10 | Medium | Mapping errors: partial capture, disputes, lost Razorpay disputes, pending rows | `amount_received` plus authorised; seven dispute values; payment fetches never clear disputes; rows only once charged; the Daily summary | [sources.md](../sources.md), [ledger-worker.md](../ledger-worker.md), [sheets-sink.md](../sheets-sink.md) |
| F11 | Medium | Singleton leases without heartbeat; the sheet can't check a token | One server process per database, by lock file; no singletons table | [ADR 0007](../decisions/0007-one-server-process-per-database.md), [operations.md](../operations.md) |
| F12 | Medium | Razorpay's event id is unsigned | Unique signed-body hash for Razorpay; a per-source request-rate cap | [Q86](../grilling-log.md), [receiver.md](../receiver.md) |
| F13 | Medium | Razorpay polling misses changes to older payments | Hourly re-fetch of unsettled payments under 7 days; disputes poll; the gap stated | [Q86](../grilling-log.md), [sources.md](../sources.md), [sweeper.md](../sweeper.md) |
| F14 | High | `BEGIN IMMEDIATE` through async SQLAlchemy; pool gives 503s | One writer connection behind a lock; a read-only connection; dialect statements in their own file | [Q83](../grilling-log.md), [operations.md](../operations.md), [ADR 0004](../decisions/0004-queue-in-the-database-behind-an-interface.md) |
| F15 | Medium | `FULL` throughput; rejections amplify floods | `FULL` kept, storm test asserts zero 503s and p99 under 2 s; rejections counted in memory | [Q83](../grilling-log.md), [Q84](../grilling-log.md), [testing.md](../testing.md), [data-model.md](../data-model.md) |
| F16 | Low | Postgres async fails on Windows | Postgres deferred; Linux only when added | [Q75](../grilling-log.md) |
| F17 | Low | Instance id, traceability of folded events, lease clock | `claimed_by`; `events.job_id`; one clock (the writer's) | [data-model.md](../data-model.md), [queue.md](../queue.md) |
| F18 | Medium | Stripe's read allowance ignored | A read budget; probes back off to 15 min; backfill and repair write listed states; hourly verify makes no source calls | [Q89](../grilling-log.md), [sources.md](../sources.md), [verify.md](../verify.md) |
| F19 | Medium | "≤10 Sheets calls for 200 events" is false | Calls grow with time, not events; the README prints the measured number | [sheets-sink.md](../sheets-sink.md), [testing.md](../testing.md) |
| F20 | Low | Five fact corrections | Stripe lists newest first; 2 MB is a recommendation, 180 s the real limit; eight dispute states; over-quota billing noted; failed-refund behaviour to be recorded | [sources.md](../sources.md), [sheets-sink.md](../sheets-sink.md), [testing.md](../testing.md) |
| F21 | High | The fakes share the design's assumptions | Recording spike; fakes replay recordings; weekly run against real services; subprocess crash points | [Q82](../grilling-log.md), [testing.md](../testing.md) |
| F22 | Medium | Invariants and crash points miss what matters | Nine invariants; settling requires no unexpected pause; more crash points | [testing.md](../testing.md) |
| F23 | Medium | Full keys allowed; one 403 pauses everything | Restricted keys required; 403 on an optional read marks columns unknown | [Q85](../grilling-log.md), [sources.md](../sources.md) |
| F24 | Medium | Admin on the public host | A second port on localhost; hardened only if made public | [Q88](../grilling-log.md), [operations.md](../operations.md) |
| F25 | Medium | Personal data and DPDP/GDPR roles | Roles stated; payloads stripped and kept 30 days; encrypted backups; no personal data in alerts | [Q91](../grilling-log.md), [hosting.md](../hosting.md), [data-model.md](../data-model.md) |
| F26 | Low | Service-account keys blocked in new organisations | OAuth fallback; `doctor` explains the error | [Q90](../grilling-log.md), [sheets-sink.md](../sheets-sink.md) |
| F27 | High | Nothing alerts when the process is down; `/ready` invites restarts | Dead-man's switch and uptime check; `/ready` = can store a delivery | [Q81](../grilling-log.md), [hosting.md](../hosting.md), [operations.md](../operations.md) |
| F28 | High | Hosting, cost, upgrades, backups, support, April 2027 | Hosting page: platform with a volume, cost in every quote, pinned versions, off-box backups, runbook, end date | [Q79](../grilling-log.md), [Q80](../grilling-log.md), [hosting.md](../hosting.md) |
| F29 | Medium | `doctor` ignores the endpoint's subscribed types and version | `doctor` checks both; the silent-webhook alert ignores unsubscribed types | [sources.md](../sources.md), [operations.md](../operations.md), [sweeper.md](../sweeper.md) |
| F30 | Critical | The schedule doesn't fit | Prototype, then a 24-28 h cut; the rest after pitching | [Q67](../grilling-log.md), [Q68](../grilling-log.md), [build-order.md](../build-order.md) |
| F31 | High | The buyers who post want app-state sync | Three buyers; the database sink with subscriptions is the first paid adapter; forwarding for n8n users | [Q66](../grilling-log.md), [Q77](../grilling-log.md), [Q78](../grilling-log.md), [next-adapters.md](../next-adapters.md) |
| F32 | High | No fee, net, method or order id | Fee, net, settlement, method, metadata keys, Daily summary | [Q69](../grilling-log.md), [sources.md](../sources.md), [sheets-sink.md](../sheets-sink.md) |
| F33 | Medium | Text dates | Typed dates, zone in the header | [Q87](../grilling-log.md), [sheets-sink.md](../sheets-sink.md) |
| F34 | Medium | Over-engineering | Postgres, payload mode, tab swap and singletons cut; admin pages, probes, Hypothesis moved after pitching | [build-order.md](../build-order.md) |
| F35 | Medium | Fifteen contradictions | Pages rewritten; earlier grilling answers marked "Changed by" | all pages, [grilling-log.md](../grilling-log.md) |
| F36 | Medium | Three scenes can't be shot | Scenes rescripted; Via column; demo allow-list | [Q93](../grilling-log.md), [showcase.md](../showcase.md) |
| F37 | Low | Glossary slips and stale status lines | Fixed; "client" in handover text means the freelance client, who is the buyer | [CONTEXT.md](../../../CONTEXT.md) |
| F38 | Low | "Proves your sheet matches Stripe" is too wide | Worded as "the last 30 days"; independent balance-transaction totals; ignored-event count shown | [verify.md](../verify.md) |
