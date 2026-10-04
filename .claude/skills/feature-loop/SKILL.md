---
name: feature-loop
description: Multi-ticket feature workflow — set a repo up, design the whole product into docs/design/ with its list of every feature, find unchecked facts in that design, plan one feature into waves of tickets, then build it unattended (workers per ticket, test, review, one user-flow check, PR merged when green), and audit the result until it converges.
disable-model-invocation: true
argument-hint: "setup | design | plan | orchestrate <feature issue> | gaps | audit [<feature issue>] | dashboard"
---

# Feature loop

One feature moves through three phases: **plan** (with the user), **build** (an orchestrator and
its workers, unattended, started by the plan itself), and **after the merge** (clean-up). Before
any feature, **design** (with the user) decides the whole product and lists every feature it
needs, in build order; plan then takes one feature from that list per run. The rules every mode
shares — phases, the wave rule, the models, the context budget, git — live in
[workflow.md](workflow.md).

`SKILL_DIR` below means this folder: `.claude/skills/feature-loop`.

**Cloud sessions.** When the environment variable `CLAUDE_CODE_REMOTE` is `true`, read
`docs/agents/cloud.md` before any mode; its rules add to this skill's.

Pick the mode from the argument. With no argument, ask which one.

## `setup` — prepare the current project

Run in the project root. Every step is safe to re-run.

1. **Detect the stack.** Read `package.json` (scripts, lockfile, test runner), `pyproject.toml`,
   `Makefile`, `.github/workflows/*`, and any `docker-compose*`/testcontainers use. Work out one
   command each for `{{SETUP_WORKTREE}}` (install deps in a fresh checkout), `{{TYPECHECK}}`,
   `{{LINT}}`, `{{ONE_TEST}}` (with a `<path>` placeholder), `{{FULL_SUITE}}`, `{{RUN_APP}}` and
   `{{APP_URL}}` (how to start the web app locally and where it answers; `none` without a web
   UI), and `{{REPO}}` (`gh repo view --json nameWithOwner --jq .nameWithOwner`). Ask the user in one round only for
   the ones still unknown; a command the project genuinely lacks becomes `none`.
2. **CLAUDE.md.** Fill `SKILL_DIR/templates/claude-md-section.md` and append it to `CLAUDE.md`
   (create the file when missing). The block sits between `<!-- feature-loop:start -->` and
   `<!-- feature-loop:end -->`; when those markers already exist, show the user the difference
   and replace only what sits between them. Every line outside the markers stays as it was.
3. **Copy files**, keeping any file the project already has (report it as kept); the one
   exception is the context guard:
   - `SKILL_DIR/templates/docs-agents/*.md` → `docs/agents/`, filling `{{REPO}}`
   - `SKILL_DIR/scripts/context-guard.py` → `scripts/claude/context-guard.py`. This file is
     harness-owned: when the project's copy differs from the skill's, overwrite it and report it
     as updated.
   - `SKILL_DIR/templates/settings.json` → `.claude/settings.json`: copy it when missing,
     otherwise merge in its two hook entries (skip one whose command already names
     `context-guard.py`) and its `attribution` key, keeping every other key.
   - add `.claude/worktrees/` and `.playwright-cli/` to `.gitignore` when absent.
4. **Labels.** With a GitHub remote, create each label with
   `gh label create <name> --color <hex> --description "<text>" --force`:
   `feature` 5319e7 "A feature spec; parent of its tickets", `ticket` 1d76db "One slice of a
   feature, built by one implementer", `in-progress` fbca04 "An implementer is working on this",
   `ready-for-agent` 0e8a16, `ready-for-human` d93f0b, `needs-info` d876e3, `needs-triage`
   ededed, `wontfix` ffffff, `audit` c5def5 "Found by the audit loop". Without a remote, skip this step and say so.
5. **Matt Pocock setup.** Read the newest
   `~/.claude/plugins/cache/claude-plugins-official/mattpocock-skills/*/skills/engineering/setup-matt-pocock-skills/SKILL.md`
   and follow it, keeping the `docs/agents/` files just written (decisions live in
   `docs/decisions/`, or in `docs/design/decisions/` when the project has a design). Then run
   `git status --short` and `git diff` and report what it added or changed.

Finish with a list of every file created, appended, merged, updated or kept, and every label
created.

## `design` — the whole product, with the user present

Read [workflow.md](workflow.md) for the git and context rules and
[prompts/docs-style.md](prompts/docs-style.md) for the page format, then run
[prompts/design.md](prompts/design.md) from step 1. It writes `docs/design/` — the overview, one
page per part, the grilling log, the decision records and `build-order.md`, the list of every
feature — and ends with a PR for the user to merge; it writes no spec, ticket or code. With an
existing `docs/design/` it extends it. If the user named a hand-off file, or the newest
`feature-loop-handoff-design-*.md` in the temp dir matches the current branch, read it and
continue from its **Next action** instead.

## `plan` — Phase A, with the user present

Read [workflow.md](workflow.md), then run Phase A from its first step; its **Design check**
picks the slice route when the project has a `docs/design/` folder. It ends by starting the build
itself, once the user approves the spec and the wave table. If the user named a hand-off file, or
the newest `feature-loop-handoff-plan-*.md` in the temp dir matches the current branch, read it
and continue from its **Next action** instead.

## `orchestrate <feature issue>` — Phase B, unattended

The plan starts this by itself; type it only to resume a build that stopped (a usage limit, an
answered `needs-info` ticket). Read [workflow.md](workflow.md) and
[prompts/orchestrator.md](prompts/orchestrator.md), then start at **Startup** for the given
feature issue. When `HERDR_ENV=1`, also read [herdr.md](herdr.md) and use its recipes to start,
watch and prompt the workers.

## `gaps` — unchecked outside facts in `docs/design/`

Read [workflow.md](workflow.md) for the git and context rules, then run
[prompts/gaps.md](prompts/gaps.md) from step 1. Without a `docs/design/` folder, say so, name
`/feature-loop design` as the way to get one, and stop.

## `audit [<feature issue>]` — Phase D, unattended

Read [workflow.md](workflow.md) and [prompts/audit.md](prompts/audit.md), then start at its
**Startup**: the whole product on `main` with no argument, one feature with its issue number.

## `dashboard` — watch the agents

Run `node .claude/skills/feature-loop/scripts/dashboard.mjs --detach` in the project root, then show the user the
URL it prints (`http://127.0.0.1:8765`; `--port <n>` changes it): open it in the session's
browser pane when it has one, otherwise print it. The page refreshes itself and shows the
accounts, every agent running now with its latest action, the current build's tickets and PR,
what finished in the last 24 hours, and the watchdog's log when there is one. It only reads, and
running it twice is harmless (the second run prints the URL). The plan's **Launch** step starts it
by itself; type this mode to bring it back, for example after a reboot.

## After a merge

The orchestrator runs Phase C of [workflow.md](workflow.md) itself after it merges the PR. When
the user merges a PR the orchestrator left open and says so, run Phase C.
