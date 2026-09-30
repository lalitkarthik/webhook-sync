# Research: Google, Python runtime, operations, Razorpay and legal gaps (G3, G11-G13, G18, G21-G24, G32, G34, G36-G41, G44)

Researched 2026-10-01 against primary sources.

Primary means developers.google.com, support.google.com and knowledge.workspace.google.com, the Google Workspace Updates blog, docs.python.org, the installed library source (uvicorn 0.54.0, filelock 4.0.7, portalocker 4.4.0, aiosqlite 0.22.1, SQLAlchemy 2.1.1), Microsoft Learn, man7.org, docs.github.com and its source repo, the GitHub API, vendor pricing pages, razorpay.com/docs and the razorpay-python source, and the Gazette of India via MeitY. Three rows were settled partly by running code on this laptop (Windows 11, CPython 3.14.7, SQLite 3.53.1); the scripts lived in a scratch folder outside the repo. The rows come from [research-gaps.md](../design/research-gaps.md). Where only a secondary source exists, it is named and not counted.

| Gap | Verdict | One-line finding | Source |
|---|---|---|---|
| G3 | confirmed | Admins can switch off outside sharing, but the default rule lets users share with anyone who has a Google Account, service accounts included. The allowlist takes only Workspace domains. A "Testing" OAuth app's refresh token dies after 7 days. | knowledge.workspace.google.com; developers.google.com/identity/protocols/oauth2; support.google.com/cloud/answer/13464323 |
| G11 | confirmed | `autocommit` still defaults to `LEGACY_TRANSACTION_CONTROL` on 3.14 and 3.15rc2. The docs say it will change to `False` without naming a version, and there's no deprecation entry. Once it's `False`, a hand-issued `BEGIN IMMEDIATE` fails. | docs.python.org/3.14 and /3.15 sqlite3; SQLAlchemy sqlite dialect docs; local run |
| G12 | confirmed | The OS drops the lock after `os._exit` and after a hard kill. On Windows the release is asynchronous: 0.3-6 ms here, and Microsoft gives no bound. filelock quietly becomes a soft lock on filesystems without `flock`. | learn.microsoft.com LockFileEx; man7 flock(2); filelock source; local run |
| G13 | confirmed | Two `uvicorn.Server`s under one `asyncio.gather` serve two apps on one loop. The second server's signal handler replaces the first's, and after a graceful stop uvicorn re-raises the signal. | uvicorn.dev; uvicorn `server.py`; local run |
| G18 | confirmed | `getByDataFilter` takes several `gridRange` filters by `sheetId` and returns a `Spreadsheet`. A `fields` mask can pick `gridProperties` and typed `effectiveValue`s. It counts as one read request. | developers.google.com/workspace/sheets/api reference and limits |
| G21 | unclear | A typed `numberValue` isn't parsed. With an explicit pattern, the pattern is used, but Google says rendering depends on the spreadsheet locale. Nothing covers the time zone. | developers.google.com/workspace/sheets/api/guides/formats, reference |
| G22 | contradicted | The limit went up to 20 million cells in September 2026 (it's "20 million cells or 100MB"). The 50,000-character rule is documented only for Excel conversion. Whether empty cells count is undocumented. | support.google.com/drive/answer/37603; workspaceupdates.googleblog.com 2026/09 |
| G23 | confirmed | A filter view changes only your own view. A view-only user can make a temporary one, which isn't saved. The menu is now Data › Create filter view. | support.google.com/docs/answer/3540681 |
| G24 | confirmed | It's Apache-2.0 and the notice reads "Copyright 2025 Supabase". The repo started at Supabase in 2021 and moved to Stripe, and there's no NOTICE file. The audit's "Stripe" is wrong. | GitHub API and `LICENSE.md` history, stripe/sync-engine |
| G32 | confirmed | The free Hobbyist plan has 20 checks, email alerts and 100 log entries per check. Period and grace each run from 1 minute to 365 days. | healthchecks.io/pricing, /docs/api |
| G34 | confirmed | GitHub Free gets 2,000 minutes a month for private repos. The multiplier page has become per-minute pricing (Linux $0.006, Windows $0.010). A weekly Windows run fits either way. | docs.github.com billing pages and their source |
| G36 | unclear | Upwork documents a 5-10 s hover preview only for **Project Catalog** cover videos, and doesn't say which seconds. Zapier's page, n8n's thread and Zapier Pro at $19.99 for 750 tasks all still hold. | support.upwork.com; help.zapier.com; community.n8n.io; zapier.com/pricing |
| G37 | confirmed | The smallest always-on options with persistent storage cost about $2-9 a month before tax: Fly.io, Railway Hobby, DigitalOcean, Hetzner and Render. | vendor pricing pages, fetched 2026-10-01 |
| G38 | unclear | The docs say `x-razorpay-event-id` is unique per event and meant for spotting duplicates. Resent webhooks need the old secret after a rotation, which implies the same signed body, but no page says the bytes match. | razorpay.com/docs/webhooks/best-practices, /validate-test |
| G39 | confirmed | There's no events API. Payments and refunds take `from`, `to`, `count` (at most 100) and `skip`. `GET /v1/disputes` exists, but its filters appear only in a CLI example. | razorpay.com/docs/api; razorpay-python resources |
| G40 | confirmed | API keys have no scopes, and only Owner and Admin can make them. There is one key set per mode. `rzp_test_`/`rzp_live_` hold. Partner OAuth offers a `read_only` scope. | razorpay.com/docs API keys, quickstart, partners OAuth |
| G41 | confirmed | `tax` is the GST, and `fee` already includes it. `refund_status` is null, `partial` or `full`. `status` is created, authorized, captured, refunded or failed. razorpay-python's webhook mock still shows `service_tax`. | razorpay.com/docs/api/payments/entity; razorpay-python `tests/mocks` |
| G44 | confirmed | The Rules and the Act's commencement were notified 13 November 2025 (G.S.R. 846(E), 843(E)). The main duties start 18 months after publication, which works out to 13 May 2027. A 12-month cut has been reported, but no amending instrument has been found. | MeitY; Gazette; PIB; Lok Sabha answer |

## G3: sharing with a service account, and the OAuth fallback

**Assumed.** A buyer's Workspace lets a sheet be shared with an outside service-account address. If it doesn't, the OAuth fallback's refresh token lasts, and only apps left in "Testing" lose it after 7 days.

**Verdict: confirmed**, with two caveats for the runbook.

**What the sources say.** The admin help page says an admin can turn off sharing outside the organisation for Docs, Sheets and Slides invitations and Drive links. They can also limit sharing to allowlisted domains, but an allowlisted domain must be a Google Workspace domain, and `iam.gserviceaccount.com` isn't one. Admins on Enterprise, Education Standard/Plus and Frontline Plus get trust rules instead. The two default rules allow internal sharing and sharing with anyone who has a Google Account, so outside sharing is on by default. The trust-rules page says service accounts fall under "Anyone with a Google Account", so a rule that blocks that condition blocks the service account too. A trust rule can instead name a group holding outside addresses, which lets an admin allow one service account on its own. Drive always marks a file as **External** when any service account can open it.

The OAuth page lists what ends a refresh token: the user revokes it, six months of disuse, more than 100 live tokens per account per client (the oldest is dropped silently), an admin scope policy (`admin_policy_enforced`) and Cloud session-length controls. A project with an **External** consent screen in **Testing** status gets tokens that expire after 7 days. The `spreadsheets` scope is **sensitive**, and `drive.file` is **non-sensitive**. Google's "verification not needed" page exempts personal-use apps (fewer than 100 users, who click through an "unverified app" screen), apps in testing, apps that only use their own service account, and **Internal** apps. An Internal app is used only inside one Workspace or Cloud Identity organisation, and that organisation must own the project. Admins can also mark an app as trusted.

**What the design should change**
- [sheets-sink.md](../design/sheets-sink.md) "Auth" and the handover runbook in [hosting.md](../design/hosting.md): when sharing fails, `doctor` should say the admin has blocked outside sharing, and give the two remedies: allow outside sharing, or (on Enterprise editions) a trust rule for a group containing the service account.
- The OAuth fallback must never run from a client in Testing status. Either the buyer creates the OAuth client in their own Workspace project with the consent screen set to **Internal**, or a production **External** client is used under the personal-use exemption (with the unverified-app click-through). With `drive.file` instead of `spreadsheets`, no sensitive-scope verification is needed, but the app can then reach only files it created or was given through the Picker. That would be a design change, so it is only noted here.
- `doctor` should tell an expired or revoked token (`invalid_grant`) apart from `admin_policy_enforced`.

URLs: https://knowledge.workspace.google.com/admin/drive/manage-external-sharing-for-your-organization, https://knowledge.workspace.google.com/admin/security/create-and-manage-trust-rules-for-drive-sharing, https://developers.google.com/identity/protocols/oauth2, https://developers.google.com/workspace/sheets/api/scopes, https://support.google.com/cloud/answer/13464323

## G11: sqlite3 `autocommit` and a hand-issued `BEGIN IMMEDIATE`

**Assumed.** On the pinned Python, sqlite3 keeps legacy transaction control as its default, so the writer can issue `BEGIN IMMEDIATE` itself through aiosqlite and SQLAlchemy.

**Verdict: confirmed.**

**What the sources say.** The 3.14 and 3.15.0rc2 docs both say `autocommit` defaults to `LEGACY_TRANSACTION_CONTROL` and will change to `False` "in a future Python release". Neither names a version. The 3.15 deprecations index has no sqlite3 entry. SQLAlchemy 2.1's SQLite dialect docs say the legacy default will last through 3.15 and names 3.16 for the change. That is secondary, and Python's own docs don't confirm it. The docs also say `isolation_level` does nothing unless `autocommit` is legacy.

aiosqlite has no `autocommit` property on its `Connection`, but `aiosqlite.connect(**kwargs)` passes keyword arguments straight to `sqlite3.connect`. SQLAlchemy documents `connect_args={"autocommit": ...}` for `sqlite+aiosqlite` for that reason.

Local run (3.14.7, SQLite 3.53.1, SQLAlchemy 2.1.1, aiosqlite 0.22.1):
- The default is `-1` (legacy), and `isolation_level` is `''`.
- Through `create_async_engine(...)`, `conn.exec_driver_sql("BEGIN IMMEDIATE")` opens a transaction, an insert follows, and `conn.commit()` ends it.
- With `autocommit=False`, `BEGIN IMMEDIATE` fails with "cannot start a transaction within a transaction".
- With `autocommit=True`, `BEGIN IMMEDIATE` works, but `commit()` does nothing and the transaction stays open.

**Open point.** When Python flips the default, the current code fails loudly: every `BEGIN IMMEDIATE` raises. Pinning `connect_args={"autocommit": sqlite3.LEGACY_TRANSACTION_CONTROL}` in `db/writer.py` today keeps the behaviour fixed across upgrades. It is the option ADR 0004 already allows for, and costs one line.

URLs: https://docs.python.org/3.14/library/sqlite3.html, https://docs.python.org/3.15/library/sqlite3.html, https://docs.python.org/3.15/deprecations/index.html, https://docs.sqlalchemy.org/en/21/dialects/sqlite.html

## G12: the lock is released when the process dies

**Assumed.** An exclusive file lock is released by the OS when the process dies, including by `os._exit`.

**Verdict: confirmed**, with two catches.

**What the sources say.** The LockFileEx page says the OS unlocks a dying process's locks, but the time this takes depends on available system resources. It recommends unlocking explicitly and warns that access may be refused until the OS has done so. flock(2) releases the lock when the last descriptor that shares it closes, and process exit closes them all. A child that inherited the descriptor keeps the lock alive. Python makes descriptors non-inheritable by default (PEP 446), and `subprocess` closes them. The filelock docs say Unix locks are released automatically on a crash.

Local run on Windows: a child took each kind of lock and then either called `os._exit(3)` or was killed with `Popen.kill()` (TerminateProcess). The parent then tried to take the lock without waiting.

| Library | After `os._exit` | After hard kill, first try | Time until free (5 kills) |
|---|---|---|---|
| filelock 4.0.7 (`WindowsFileLock`, LockFileEx) | free | free | 0.8-2.2 ms |
| portalocker 4.4.0 | free | refused | 0.3-5.9 ms |
| `msvcrt.locking` | free | refused | 0.3-6.2 ms |

The second catch is in filelock's source. On a Unix filesystem without `flock` (ENOSYS, as on some network mounts), `UnixFileLock` silently switches to a soft lock by default (`fallback_to_soft=True`). A soft lock is only a marker file, and it survives a crash.

**What the design should change**
- [ADR 0007](../design/decisions/0007-one-server-process-per-database.md) and `lockfile.py`: acquire with a short timeout (about 2 s), not one try, so a restart straight after a kill doesn't report "another server is running".
- Build the lock with `fallback_to_soft=False`, so an unsupported filesystem fails closed instead of leaving a lock that stays after a crash. No PID check is needed.

URLs: https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex, https://man7.org/linux/man-pages/man2/flock.2.html, https://docs.python.org/3/library/msvcrt.html, https://py-filelock.readthedocs.io/en/latest/, https://github.com/tox-dev/filelock/blob/main/src/filelock/_unix.py

## G13: two uvicorn listeners in one process

**Assumed.** uvicorn can serve the public port on all interfaces and the admin port on 127.0.0.1 from one process and one event loop.

**Verdict: confirmed.**

**What the sources say.** uvicorn's docs say to use `uvicorn.Config` and `uvicorn.Server`, and to await `Server.serve()` from a running loop. They don't mention running two servers. In the source (0.54.0), `serve()` wraps itself in `capture_signals()`. That installs `handle_exit` for SIGINT, SIGTERM and, on Windows, SIGBREAK with `signal.signal`, keeping the previous handlers. On exit it restores them and **re-raises** each signal it caught.

Local run: two apps, public on `0.0.0.0:18081` and admin on `127.0.0.1:18082`, under one `asyncio.gather`. Both answered on one loop in one thread, and each app ran its lifespan once.
- Setting `should_exit = True` on both stopped both cleanly, and `asyncio.run` returned.
- One SIGINT reached only the server that started second, whose handler had replaced the first's. That server shut down, restored the first server's handler and re-raised SIGINT. The first then shut down, restored Python's default handler and re-raised again. The run ended in `KeyboardInterrupt`, with `gather` cancelled.

On Linux, SIGTERM would be re-raised into the default action, which ends the process at once, so nothing after `gather` would run.

**Open point.** The shutdown sequence in [operations.md](../design/operations.md) (edge case I5: 503s, 20 s for running jobs, release leases, release the lock) must not live after `gather`. Install one handler of our own that sets `should_exit` on both servers and starts the drain. Disable uvicorn's handling by overriding `capture_signals` with a no-op context manager. That method is internal, not a documented API, so pin uvicorn and cover shutdown in the weekly Windows integration run.

URLs: https://uvicorn.dev/, https://github.com/encode/uvicorn/blob/master/uvicorn/server.py

## G18: one `getByDataFilter` read per flush

**Assumed.** `spreadsheets.getByDataFilter` with a GridRange by `sheetId` returns grid size, the header row and the key column in one unformatted call.

**Verdict: confirmed** from the reference. It hasn't been run against a live sheet yet.

**What the sources say.** The method is `POST …/spreadsheets/{id}:getByDataFilter` and returns a `Spreadsheet`. With several `dataFilters`, it returns the parts of the spreadsheet that any filter matches. `includeGridData` is ignored when a field mask is set, so the mask itself must ask for `sheets.data`. A `GridRange` names a `sheetId`, and a missing index leaves that side open. One filter for row 0 and one for the key column therefore covers both. A mask such as `sheets(properties(sheetId,gridProperties(rowCount,columnCount)),data(startRow,startColumn,rowData(values(effectiveValue))))` returns sizes and typed values. `effectiveValue` is an `ExtendedValue` (`numberValue`, `stringValue` and so on), with no formatting applied. `formattedValue` is a separate field. The limits page counts any method that retrieves data as a read, at 60 per minute per user and 300 per project. By that definition this POST is one read.

**Open point.** In the first Sheets slice, check two things against a live sheet: that each filter comes back as its own entry in `data[]`, and what a filter naming a deleted `sheetId` returns.

URLs: https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/getByDataFilter, https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/cells, https://developers.google.com/workspace/sheets/api/limits

## G21: date serials and locale

**Assumed.** A `numberValue` date serial with a DATE_TIME format shows correctly in every spreadsheet locale and is never re-parsed.

**Verdict: unclear**, though it leans towards yes.

**What the sources say.** Dates are doubles counted from 30 December 1899. A typed `numberValue` is stored as a number, and parsing applies only to user-entered input. If `NumberFormat.pattern` is empty, Sheets uses a default pattern for the locale. The formats guide says the rendered result of a pattern still depends on the spreadsheet's `locale`, and its examples assume `en_US`. `SpreadsheetProperties.timeZone` exists, but no page says whether it shifts how a stored serial is displayed.

**Spike.** In the recording spike, write one serial with pattern `yyyy-mm-dd hh:mm:ss`. Change the sheet's locale to `de_DE`, `ja_JP` and `hi_IN`, and its time zone, with `updateSpreadsheetProperties`. Read `formattedValue` back each time, and keep the pattern digits-only (no `mmm` or AM/PM) whatever the result.

URLs: https://developers.google.com/workspace/sheets/api/guides/formats, https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets

## G22: cell and character limits

**Assumed.** A cell holds at most 50,000 characters. A spreadsheet holds at most 10 million cells, empty grid cells included.

**Verdict: contradicted.**

**What the sources say.** The Drive help page now says "Up to 20 million cells or 100MB" for Sheets. The Workspace Updates blog made the doubled limit generally available for all editions and personal accounts, with no setting to change. It began rolling out to Rapid Release domains on 10 September 2026 and to Scheduled Release domains on 28 September 2026, each over up to 15 days. On the same page, 50,000 characters appears only as the rule that drops longer cells when an Excel file is converted. The in-app error "more than the maximum of 50000 characters in a single cell" is seen only in community threads, which are secondary. No primary source says whether empty grid cells count.

**What the design should change**
- [sheets-sink.md](../design/sheets-sink.md), in the alerts table ("Spreadsheet past 80 % of 10 million cells"): make the ceiling a setting that defaults to 10 million until mid-October 2026, then 20 million, and also watch the 100 MB bound. Better still, treat the API's cell-limit error on `batchUpdate` as "sheet full" whatever the number.
- Keep the 50,000-character cut: it is harmless even if the true limit is higher.
- Spike (a throwaway sheet): grow an empty tab's grid past the limit with `appendDimension`, and record the error text and number. That settles whether empty cells count.

URLs: https://support.google.com/drive/answer/37603, https://workspaceupdates.googleblog.com/2026/09/doubled-cell-limits-in-google-sheets-now-generally-available.html, https://workspaceupdates.googleblog.com/2026/04/faster-performance-and-doubled-cell-limits-in-Google-Sheets.html. Secondary, not counted: https://support.google.com/docs/thread/10999557

## G23: filter views

**Assumed.** A filter view changes only the viewer's own view, and a view-only buyer can still make a temporary one.

**Verdict: confirmed.**

**What the sources say.** The help page says an ordinary filter shows up for everyone with access, and a filter view applies only to your own view "on a computer". A view-only user "can create a temporary filter view that only you can use", and it isn't saved. The steps are now **Data › Create filter view**, then Save view, and **Data › Change view** to pick a saved one. A saved view can be shared as a link. The page doesn't say whether saved views are listed for every collaborator, or whether the mobile apps support filter views.

**What the design should change**
- The header note in [sheets-sink.md](../design/sheets-sink.md) and [ADR 0006](../design/decisions/0006-machine-owned-tabs.md): "Sort and filter with Data › Create filter view" rather than "Data › Filter views".
- In [hosting.md](../design/hosting.md) FAQ, add "on a computer".

URL: https://support.google.com/docs/answer/3540681

## G24: sync-engine's licence and copyright holder

**Assumed.** sync-engine is Apache-2.0, and its copyright holder is either Stripe (as the audit says) or "Copyright 2025 Supabase" (as the inventory says).

**Verdict: confirmed.** The holder is Supabase.

**What the sources say.** The GitHub API reports `stripe/sync-engine` as Apache-2.0, not a fork, created 29 April 2021. `supabase/stripe-sync-engine` redirects to the same repository id, so the repo was transferred. The first commits are by Paul Copplestone: "init the repo" on 29 April 2021, then "Switch license to apache 2" on 1 May 2021. The only later change to `LICENSE.md` (commit 7acaa382, 5 April 2025) moved the appendix line from "Copyright 2019 Supabase" to "Copyright 2025 Supabase". The repo has no NOTICE file and no per-package licence files. The ported file `packages/destination-google-sheets/src/index.ts` has no header. `apps/engine/package.json` names Stripe as author. The README says active work happens in `stripe/sync-engine-fork`, also Apache-2.0.

**What the design should change**
- The "derived from" header for ported files and `THIRD_PARTY_NOTICES.md` ([ADR 0005](../design/decisions/0005-permissive-licences-only.md)): quote "Copyright 2025 Supabase" and the Apache-2.0 text from `LICENSE.md` at the pinned commit (0cb8ca4). Name the source as `stripe/sync-engine`. Don't call Stripe the copyright holder.

URLs: https://github.com/stripe/sync-engine/blob/main/LICENSE.md, https://api.github.com/repos/stripe/sync-engine/commits?path=LICENSE.md

## G32: Healthchecks.io free tier

**Assumed.** The free tier covers one check per deployment and alerts by email when pings stop.

**Verdict: confirmed.**

**What the sources say.** The Hobbyist plan is free and gives 20 checks, 100 log entries per check and email alerts, with no SMS or phone credits. The pricing page asks users not to open several accounts to get around the limits. A check goes down when no ping arrives within period plus grace. The API accepts a timeout and a grace of 60 s to 31,536,000 s each, so 30 minutes is valid. Supporter, at $5 a month, has the same limits.

**Open point.** If the checks live in Lalit's account, 20 checks means 20 deployments. After that, each buyer holds the check in their own account, which fits "the client owns every account" in [hosting.md](../design/hosting.md).

URLs: https://healthchecks.io/pricing/, https://healthchecks.io/docs/configuring_checks/, https://healthchecks.io/docs/api/

## G34: GitHub Actions minutes for the weekly Windows run

**Assumed.** GitHub bills Windows runner minutes at a multiplier for private repos, and the weekly Windows run fits the free minutes.

**Verdict: confirmed**, because the run fits whatever the multiplier. The multiplier wording itself is gone.

**What the sources say.** GitHub Free includes 2,000 minutes, 500 MB of artifact storage and 10 GB of cache per repo each month for private repositories. Public repositories and self-hosted runners are free. The old "minute multipliers" page now redirects to "Actions runner pricing", which lists per-minute prices: Linux 2-core $0.006, Windows 2-core $0.010, macOS $0.062. That page was last reworked for the 1 January 2026 price change. Neither page now says how Windows minutes draw on the included quota, and the billing dashboard may show spend instead of minutes. Under the old 2x rule, a 15-minute weekly run uses about 130 of the 2,000 minutes a month.

URLs: https://docs.github.com/en/billing/concepts/product-billing/github-actions, https://docs.github.com/en/billing/reference/actions-runner-pricing

## G36: the Upwork preview and the README's claims

**Assumed.** Upwork previews only a video's first 5-10 s, and the README's cited claims still hold.

**Verdict: unclear** for Upwork. The three README claims are confirmed.

**What the sources say.**
- **Upwork.** Two help articles say that when a video is a **Project Catalog** project's cover, 5-10 seconds autoplay when a client hovers. A catalog video can be up to 60 s, 100 MB, MP4, one per project. Neither article says which seconds play. No Upwork page on portfolio or proposal videos mentions a preview.
- **Zapier.** "Google Sheets: Zap is creating duplicate rows" is live (updated 22 August 2025). It blames large sheets with Autoreplay and unused columns. A sibling page covers rows that mix or overwrite each other under concurrent runs, and may suit the README better.
- **n8n.** The thread "Google Sheets 'Append Row' drops rows under high webhook concurrency" (23 January 2026) is live and now closed. It reports about 100 executions, all green, with the sheet stopping at about 56 or about 63 rows.
- **Zapier Pro.** The pricing page shows Professional "Starting from $19.99/month". Its embedded plan data has "Pro 750" at $239.88 a year ($19.99 a month) and $29.99 month to month.

**What the design should change**
- [showcase.md](../design/showcase.md) and Q56: state the 5-10 s preview as a Project Catalog cover behaviour, and keep "result first" as good practice anyway. Say "about 56-63 of 100" and "billed annually" in the README.

URLs: https://support.upwork.com/hc/en-us/articles/1500011309082, https://support.upwork.com/hc/en-us/articles/4402033684115, https://help.zapier.com/hc/en-us/articles/21637060333325-Google-Sheets-Zap-is-creating-duplicate-rows, https://help.zapier.com/hc/en-us/articles/21637311927309, https://community.n8n.io/t/google-sheets-append-row-drops-rows-under-high-webhook-concurrency/254786, https://zapier.com/pricing

## G37: always-on hosting with a persistent volume

**Assumed.** Platforms with an always-on container and a persistent volume cost about $5-12 a month.

**Verdict: confirmed.** The cheapest options now fall below $5.

**What the sources say** (list prices on 2026-10-01, before tax):

| Platform | Smallest always-on option | Storage | Monthly |
|---|---|---|---|
| Fly.io | shared-cpu-1x, 256-512 MB (price varies by region) | volume $0.15/GB | about $2-3.50 + about $0.15; card required, no free tier |
| Railway | Hobby plan, $5 including $5 of usage (RAM about $10/GB-month, volume about $0.16/GB-month) | volume | $5 for a small service |
| DigitalOcean | Basic Droplet 512 MB / 10 GB, or 1 GB / 25 GB | local SSD | $4 or $6 |
| Hetzner Cloud | CX23: 2 vCPU, 4 GB, 40 GB NVMe, 20 TB traffic, IPv4 included | local disk | from €5.99, excl. VAT; not stocked in every location |
| Render | Starter: 512 MB, under 1 CPU | disk $0.25/GB; paid instances only, and a disk blocks zero-downtime deploys | $7 + disk |

Render's free instances spin down and can't take a disk. Railway's free trial has an idle timeout.

**Open point.** [hosting.md](../design/hosting.md) can keep "$5-12" as a safe quote range. Re-read the vendor page on the day of each quote, as it already says.

URLs: https://docs.fly.io/about/pricing/, https://railway.com/pricing, https://www.digitalocean.com/pricing/droplets, https://www.hetzner.com/cloud/cost-optimized, https://render.com/pricing, https://render.com/docs/disks

## G38 (SPIKE): what a Razorpay retry resends

**Assumed.** A genuine Razorpay retry resends the same body.

**Verdict: unclear, needs the spike.**

**What the sources say.** The best-practices page says delivery is at-least-once. A response slower than 5 s counts as a failure and is resent. Retries back off exponentially for 24 hours, and then the webhook is disabled. To spot duplicates, it points to `x-razorpay-event-id`, whose value is "unique per event". That reads as the same id on every resend of one event. The validate-and-test page says to keep checking resends of older requests with the **old** secret after a rotation. So each resend carries the signature made at first send, which suggests the same payload, but no page says the body bytes match.

**Spike.** With a test-mode webhook pointed at a tunnel that returns 500 twice, then 200, log the raw body's SHA-256 and `x-razorpay-event-id` for each attempt. Also resend once from the Dashboard, if it offers that. If the bytes differ, the signed-body-hash dedupe ([sources.md](../design/sources.md), Q86) only catches replays, and the event id plus fingerprint must carry retries.

URLs: https://razorpay.com/docs/webhooks/best-practices, https://razorpay.com/docs/webhooks/validate-test, https://razorpay.com/docs/webhooks/faqs/

## G39: Razorpay list endpoints

**Assumed.** Razorpay has no events list. Payment and refund lists filter by creation time (`from`, `to`, `count` of at most 100, `skip`), and `GET /v1/disputes` exists.

**Verdict: confirmed**, with one open point.

**What the sources say.** "Fetch All Payments" (`GET /v1/payments`) takes `from` and `to` (Unix seconds), `count` (default 10, at most 100) and `skip`. "Fetch All Refunds" (`GET /v1/refunds`) takes the same four, with `from`/`to` on creation time. The API reference has no events resource, and razorpay-python has none either. Its resources run from account to webhook, with no `event`. "Fetch All Disputes" (`GET /v1/disputes`) exists and returns a collection whose statuses are `open`, `under_review`, `won`, `lost` and `closed`. The page lists **no** query parameters. Only its CLI sample passes `--count`, `--skip`, `--from` and `--to`, and its errors mention `expand`.

**Open point.** In the Razorpay slice, confirm that `/v1/disputes` honours `count`, `skip` and `from`. Until then the hourly poll pages with `count`/`skip` and filters by `created_at` itself.

URLs: https://razorpay.com/docs/api/payments/fetch-all-payments/, https://razorpay.com/docs/api/refunds/fetch-all/, https://razorpay.com/docs/api/disputes/fetch-all/, https://github.com/razorpay/razorpay-python/tree/master/razorpay/resources

## G40: Razorpay keys

**Assumed.** Razorpay keys can't be restricted to reading. Test keys start `rzp_test_`.

**Verdict: confirmed.**

**What the sources say.** An API key is a `key_id` and a `key_secret`, generated per mode under Account & Settings › API Keys. It works for every website and app on the merchant id, and "you can use only one set of API keys". Only the Owner and Admin roles can see or generate keys, and Razorpay has no developer role. No page describes any scope or permission on a key; this is an absence, not a statement. The quickstart says test keys begin `rzp_test_` and live keys `rzp_live_`. Keys can be rolled either at once or after 24 hours. Razorpay **partner** OAuth does offer a `read_only` scope (every GET). Its access token lasts 90 days and comes with a refresh token, but it needs a Technology Partner application.

**What the design should change**
- [sources.md](../design/sources.md) "Keys" and [hosting.md](../design/hosting.md) Privacy: "rotated at handover" rolls the buyer's **only** key set, which their checkout also uses. The runbook should use the 24-hour roll and tell the buyer to update their other integrations. Otherwise it should say rotation is optional.
- Note the partner-OAuth `read_only` route in [next-adapters.md](../design/next-adapters.md) as the way to get a read-only Razorpay credential later.

URLs: https://razorpay.com/docs/payments/dashboard/account-settings/api-keys/, https://razorpay.com/docs/api/authentication/, https://razorpay.com/docs/payments/quickstart/, https://razorpay.com/docs/partners/technology-partners/onboard-businesses/integrate-oauth/integration-steps/

## G41: Razorpay payment fields

**Assumed.** The payment's GST-on-fee field is `tax`, not `service_tax` as the mock shows, and `refund_status` is null, `partial` or `full`.

**Verdict: confirmed.**

**What the sources say.** On the India payments entity page, `fee` is the fee charged by Razorpay, including GST, and `tax` is the GST charged for the payment. `refund_status` is `null`, `partial` or `full`. `status` is `created`, `authorized`, `captured`, `refunded` or `failed`. The docs' own webhook samples use `tax`. razorpay-python's `tests/mocks/fake_payment_authorized_webhook.json` still carries `service_tax`, which is probably where the mock came from, while its other payment mocks use `tax`.

**What the design should change**
- [sources.md](../design/sources.md) "Fetch": `created` isn't mapped. A sweep can list payments that never reached `authorized`, so map `created` to a pending state or skip it. Net is `amount - fee`, not `amount - fee - tax`, because `fee` already includes the GST.

URLs: https://razorpay.com/docs/api/payments/entity/, https://razorpay.com/docs/webhooks/payloads/payments/, https://github.com/razorpay/razorpay-python/tree/master/tests/mocks

## G44: DPDP Rules dates

**Assumed.** India's DPDP Rules were notified on 13 November 2025, with substantive obligations from about 13 May 2027.

**Verdict: confirmed.**

**What the sources say.** The Gazette of India Extraordinary for 13 November 2025 carries two notifications. G.S.R. 846(E) makes the DPDP Rules, 2025. Rules 1, 2 and 17-21 took effect on publication. Rule 4 (consent managers) starts one year later. Rules 3, 5-16, 22 and 23 start eighteen months after publication. G.S.R. 843(E) brings the Act into force in the same three steps, with the main duties (sections 3-17 and 27-34) at eighteen months. MeitY's library lists both dated 14 November 2025, with a corrigendum on 16 December 2025. PIB's release of 14 November 2025 speaks of an 18-month phased timeline. A Lok Sabha answer gives the notification date as 13 November 2025. Neither instrument names a calendar date: 13 November 2026 and 13 May 2027 are computed from them.

Secondary, not counted: Business Standard and Storyboard18 (January 2026) reported that MeitY proposed cutting the 18 months to 12. That would move the main duties to about 13 November 2026, six weeks from today. One tracker's review on 6 September 2026 found no amending instrument.

**What the design should change**
- The README and runbook privacy text ([hosting.md](../design/hosting.md), Q91): say "main duties from 13 May 2027 under the Rules as notified; check for amendments". Re-check the Gazette before the first Indian quote.

URLs: https://www.meity.gov.in/documents/act-and-policies/digital-personal-data-protection-rules-2025-gDOxUjMtQWa, https://www.pib.gov.in/PressReleasePage.aspx?PRID=2190014, https://sansad.in/getFile/lsapps/loksabhaquestions/annex/188/AU3943_458sox.pdf, https://egazette.gov.in (CG-DL-E-14112025-267647). Secondary, not counted: https://www.business-standard.com/technology/tech-news/meity-may-cut-compliance-timeline-for-key-dpdp-rules-to-12-months-126012201293_1.html, https://dpdprules.org/blog/dpdp-18-months-to-12-months-proposal

## Sources

Each section above gives its full URLs. Grouped by owner:

- **Google:** knowledge.workspace.google.com (external sharing; trust rules); developers.google.com/identity/protocols/oauth2; developers.google.com/workspace/sheets/api (scopes, getByDataFilter, spreadsheets, cells, formats guide, limits); support.google.com/cloud/answer/13464323; support.google.com/drive/answer/37603; support.google.com/docs/answer/3540681; workspaceupdates.googleblog.com (2026/04 and 2026/09 cell-limit posts).
- **Python runtime:** docs.python.org/3.14 and /3.15 (`sqlite3`, deprecations, `msvcrt`); docs.sqlalchemy.org/en/21/dialects/sqlite.html; learn.microsoft.com LockFileEx; man7.org flock(2); py-filelock.readthedocs.io and filelock `_unix.py`; uvicorn.dev and uvicorn `server.py`.
- **Ops and market:** github.com/stripe/sync-engine (`LICENSE.md` and its history, through the GitHub API); healthchecks.io (pricing, configuring checks, API); docs.github.com (Actions billing, runner pricing); support.upwork.com articles 1500011309082 and 4402033684115; help.zapier.com articles 21637060333325 and 21637311927309; community.n8n.io topic 254786; zapier.com/pricing; docs.fly.io/about/pricing; railway.com/pricing; digitalocean.com/pricing/droplets; hetzner.com/cloud/cost-optimized; render.com/pricing and /docs/disks.
- **Razorpay:** razorpay.com/docs (webhooks best practices, validate-test, FAQs, payment payloads; API payments fetch-all, refunds fetch-all, disputes fetch-all, payments entity, authentication; dashboard API keys; quickstart; partner OAuth integration steps); github.com/razorpay/razorpay-python (resources, `tests/mocks`, commit 8960507).
- **Legal:** meity.gov.in DPDP Rules 2025 page; Gazette of India Extraordinary No. 757 and 760 of 13 November 2025 (G.S.R. 843(E), 846(E)); pib.gov.in PRID 2190014; sansad.in Lok Sabha answer AU3943.
- **Local runs, 2026-10-01:** CPython 3.14.7 on Windows 11 with filelock 4.0.7, portalocker 4.4.0, uvicorn 0.54.0, aiosqlite 0.22.1, SQLAlchemy 2.1.1 and SQLite 3.53.1.
- **Secondary, not counted:** support.google.com/docs/thread/10999557; Business Standard, 22 January 2026 (DPDP 12-month proposal); dpdprules.org review of 6 September 2026.
