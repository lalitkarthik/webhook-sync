# Local sessions

How this repo is worked on in Claude Code on the owner's own machine (Windows 11, Git Bash, or
any Unix shell). Read this first in any session where `CLAUDE_CODE_REMOTE` is not `true`. Its
rules add to the feature loop's (`.claude/skills/feature-loop/workflow.md`); the cloud
equivalent is [cloud.md](cloud.md).

## One-time setup

| Need | How | Check |
|---|---|---|
| Git and the repo | `git clone https://github.com/lalitkarthik/webhook-sync` | `git remote -v` names `lalitkarthik/webhook-sync` |
| GitHub CLI, signed in | install `gh`, then `gh auth login` (scope `repo`) | `gh auth status` |
| uv | install from astral.sh; it installs Python 3.14 from `.python-version` | `uv --version` |
| A real Python 3 on `PATH` for the hooks | the context-guard hook runs `python` or `python3`; on Windows, make sure it is not the Microsoft Store stub (`uv python install 3.14` and put it on `PATH`, or install python.org's) | `python -c "import sys; print(sys.version)"` |
| Bash for the hooks | on Windows, Git for Windows provides it; Claude Code runs hooks with it | `bash --version` |
| Dependencies | `uv sync` in the repo root | `uv run python -V` prints 3.14.x |
| Secrets in `.env` | copy the names from `.env.example`; `python scripts/set_env.py google <service-account.json>` writes `GOOGLE_SERVICE_ACCOUNT_B64`; add the three `RAZORPAY_*` test-mode values; `.env` is git-ignored | `python scripts/set_env.py show` lists the names, never values |
| Claude accounts runner | `~/.claude/accounts/cx.mjs` (Node): the loop starts the orchestrator, every implementer and the flow checker with it | `node ~/.claude/accounts/cx.mjs pick` prints an account |
| Reviewer and auditor definitions | `~/.claude/agents/fl-opus-medium.md` and `fl-opus-high.md` | both files exist |
| Optional: Herdr | `HERDR_ENV=1` switches the loop to Herdr tabs (`.claude/skills/feature-loop/herdr.md`) | — |

Only `wsync serve` and the live research checks need the secrets; the test suite never calls
Razorpay or Google.

## Smoke test, at the start of a session

Run once per new machine or after a long break, and report each line:

| Check | Command | If it fails |
|---|---|---|
| Skills present | `.claude/skills/feature-loop/SKILL.md` exists and `/mp-research` is listed | `git pull` on a branch that has `.claude/skills/` |
| Hooks run | after the first tool call, no hook error is shown | fix the Python/bash rows above |
| GitHub | `gh api 'repos/{owner}/{repo}/issues?per_page=1' --jq length` | `gh auth login` |
| Python | `uv run python -V` | `uv python install 3.14 && uv sync`; never lower the version |
| Branch is green | `uv run pyright`, `uv run ruff check . && uv run ruff format --check .`, `uv run pytest` | report the failure before planning or building on it |
| Worktrees | `git worktree add ../wt-smoke -b smoke/wt && git worktree remove ../wt-smoke && git branch -D smoke/wt` | run implementers one at a time in the main checkout |
| Secrets (only when a step needs live calls) | `python scripts/set_env.py show`; `RAZORPAY_KEY_ID` starts `rzp_test_` | set them as above; a `rzp_live_` key: stop and tell the owner |

## Rules that differ from the cloud

1. **`gh` works fully.** GraphQL is not blocked locally, so `gh issue …` and `gh pr …` work; the
   REST forms in `docs/agents/issue-tracker.md` work too and are fine to keep using.
2. **Branch deletion works.** Phase C deletes the feature branch on GitHub as written.
3. **Temp files survive** between sessions on the same machine: hand-off and state files go in
   the temp dir (`python -c "import tempfile; print(tempfile.gettempdir())"`). Still mirror
   the orchestrator's state and every hand-off into a comment on the feature issue, so a cloud
   session or another machine can continue.
4. **Real deliveries are possible** through the tunnel in `docs/design/hosting.md` (zrok,
   `*.shares.zrok.io`). Tests still never depend on one.
5. **Secrets** come from `.env`. Never print a value, write one into a tracked file, or put
   one in a commit, issue, PR or log. Test-mode keys only.
6. **Sub-agents run in parallel** wherever the work is independent; none starts agents of its
   own (CLAUDE.md).
7. **No attribution lines** on commits or PRs.

## The cycle, per build step

One step of `docs/design/build-order.md` at a time, each in its own sessions:

1. **Plan** (one session): `/feature-loop plan <step>`; ends with the feature issue, its tickets
   and waves, and one approval from the owner. On approval it starts the build itself
   (`cx run --detach yes --name orch-<slug>`) and posts the orchestrator hand-off on the issue.
2. **Build** (unattended): the orchestrator starts a worker per ticket, tests and reviews each,
   walks the feature's user flows once, opens one PR and merges it when every ticket is closed,
   the suite is green and the flow check passed. Progress shows live on the dashboard the plan
   opens (`/feature-loop dashboard` brings it back: every agent, its latest action and each
   ticket), on the feature issue and in `~/.claude/accounts/runs/orch-<slug>.json`. To resume a
   stopped build:
   `/feature-loop orchestrate #<feature issue>`.
3. **Merge**: a PR the orchestrator left open (it says why) is merged by the owner.
4. **Clean up**: the orchestrator runs Phase C after its own merge; after an owner merge, tell a
   session "the PR is merged". Then plan the next step. `/feature-loop audit` checks a merged
   feature or the whole product.
