# Cloud sessions

How this repo is worked on in a Claude Code cloud session (claude.ai/code, or "Cloud" in the
desktop app). Read this first whenever `CLAUDE_CODE_REMOTE` is `true`. Its rules add to the
feature loop's (`.claude/skills/feature-loop/workflow.md`).

## What a cloud session has

- **This repo only.** Nothing from the owner's laptop: no user-level skills, plugins, memory or
  `~/.claude/CLAUDE.md`. Everything the loop needs is committed here:
  - `.claude/skills/feature-loop/`: the loop (`/feature-loop plan | orchestrate #<n> | gaps`).
  - `.claude/skills/mp-*/`: the Matt Pocock skills the loop calls, vendored under the MIT
    licence (`.claude/skills/MATT-POCOCK-SKILLS-LICENSE`): `/mp-grilling`, `/mp-research`,
    `/mp-to-spec`, `/mp-to-tickets`, `/mp-code-review`, `/mp-resolving-merge-conflicts`,
    `/mp-prototype`, `/mp-tdd`, `/mp-domain-modeling`, `/mp-diagnosing-bugs`.
  - `.claude/settings.json`: the context-guard hooks and the session-start script.
- **`gh` and `git`** through GitHub's proxy, already signed in. Branch deletion and tag pushes
  are blocked there; leave Phase C's branch clean-up to the owner. GitHub's GraphQL API is
  blocked, so every `gh issue`/`gh pr`/`gh label` command fails: use the `gh api` REST forms in
  `docs/agents/issue-tracker.md` wherever the loop names one.
- **Secrets as environment variables**, set by the owner in the cloud environment (names in
  `.env.example`). The session-start output says which are set. There is no `.env` file.
- **No project MCP servers.** `.mcp.json` is not loaded in the cloud.

## Rules that add to the loop

1. **Nothing survives outside GitHub.** The machine can be reclaimed after it idles, and the
   temp directory goes with it.
   - Push `feature/<slug>` after every commit on it: planning commits and every merged ticket.
   - Before parking a ticket or ending a turn with implementers running, push each
     `ticket/<n>-<slug>` branch that has commits.
   - Keep the orchestrator's state file as the loop says, and also mirror it into one comment
     on the feature issue that begins `Orchestrator state` (edit the same comment each time).
     On startup with no state file, rebuild it from that comment.
   - Write a hand-off into a comment on the feature issue as well as the temp file.
2. **The extra-accounts route does not exist here.** `~/.claude/accounts/cx.mjs` is missing, so
   every implementer is a background `Agent` call with `isolation: "worktree"`.
3. **No inbound traffic.** Razorpay cannot deliver a webhook to this machine. Tests run on
   fakes and recordings. Outbound calls to Razorpay's test API and the Google Sheets API work
   when the keys are set and the network setting allows those hosts. Test payments can be paid
   here, headless through Checkout's mock bank page (`wsync demo`, Q105). Anything that needs a
   real delivery, a tunnel or a screen recording is the owner's: label that
   ticket `ready-for-human` and say exactly what to run.
4. **Secrets.** Read them from the environment. Never print a value, write one to a file in the
   repo, or put one in a commit, issue, PR or log. Test-mode keys only: if `RAZORPAY_KEY_ID`
   starts `rzp_live_`, stop and tell the owner.
5. **No attribution lines** on commits or PRs, as everywhere in this repo.
6. **First thing in a new environment: the smoke test below.** If a step fails, say which, and
   use the fallback beside it.

## Research

Use primary sources (official docs, source code, specs) and save each note to
`docs/research/<topic>.md` with its sources, as `/mp-research` says.

| Tool | Use it for | Needs |
|---|---|---|
| `WebSearch` | Finding the page | Nothing. About 200 searches a session, shared with subagents. |
| `WebFetch` | Reading a known URL | The network setting must allow the host ("Full" allows all). |
| `curl` | Raw API docs, OpenAPI files, GitHub raw files | The same network setting |
| `firecrawl search` / `firecrawl scrape <url>` | Pages WebFetch cannot read (heavy JavaScript, blocks) | `FIRECRAWL_API_KEY` set; the session-start script installs the CLI. Credits are limited: use it last. |
| A local clone (`git clone --depth 1`) | Reading a library's source | GitHub is always reachable |

Research subagents run in parallel when their questions are independent; none may start agents of its own.

## Smoke test for a new environment

Run once and report the result of each line:

| Check | Command | If it fails |
|---|---|---|
| Skills loaded | `/mp-research` appears in the skill list, and `.claude/skills/feature-loop/SKILL.md` exists (`/feature-loop` sets `disable-model-invocation`, so it is typed by the owner and never listed for the agent) | The repo was not cloned at a commit that has `.claude/skills/` |
| Hooks run | The session-start lines ("env … set") are in context | Hooks only run in single-repo sessions, and on a fresh start may not reach the first turn: run `bash scripts/claude/cloud-session-start.sh` by hand |
| GitHub issues | `gh api 'repos/{owner}/{repo}/issues?per_page=1' --jq length` | Ask the owner to install the Claude GitHub app on the repo. (`gh issue list` always fails here: GraphQL is blocked in cloud sessions; use the REST forms in `docs/agents/issue-tracker.md`) |
| Sub-issue and dependency API | `gh api repos/lalitkarthik/webhook-sync/issues?per_page=1 --jq '.[0].number'` | Fall back to `## Parent` and `## Blocked by` text in ticket bodies, which the loop already writes |
| Python | `uv python install 3.14 && uv run --python 3.14 python -V` | Tell the owner; do not lower the Python version silently |
| Network | `curl -s -o /dev/null -w '%{http_code}\n' https://api.razorpay.com` and the same for `https://sheets.googleapis.com` (any HTTP code means reachable; `head -1` shows only the proxy's `Connection Established`) | The owner sets network access to "Full" or adds the hosts |
| Worktrees | `git worktree add ../wt-smoke -b smoke/wt && git worktree remove ../wt-smoke && git branch -D smoke/wt` | Run implementers one at a time in the main checkout |

## What the owner sets up (once, in the cloud environment)

- Network access: **Full**, so research can read any site. Narrower: "Custom" with the trusted
  defaults plus `api.razorpay.com`, `razorpay.com`, `sheets.googleapis.com`,
  `oauth2.googleapis.com`, `www.googleapis.com`, `api.firecrawl.dev`.
- Environment variables: the names in `.env.example`, test-mode values only.
- No setup script is needed: `scripts/claude/cloud-session-start.sh` runs at every session start.
