---
name: feature-loop
description: Multi-ticket feature workflow — set a repo up, find unchecked facts in a design, plan a feature into waves of tickets, then orchestrate background implementers to build it.
disable-model-invocation: true
argument-hint: "setup | plan | orchestrate <feature issue> | gaps"
---

# Feature loop

One feature moves through three phases: **plan** (with the user), **build** (the main session
orchestrates implementers, unattended), and **after the merge** (clean-up). The rules every mode
shares — phases, the wave rule, the context budget, git — live in [workflow.md](workflow.md).

`SKILL_DIR` below means this folder: `.claude/skills/feature-loop`.

**Cloud sessions.** When the environment variable `CLAUDE_CODE_REMOTE` is `true`, read
`docs/agents/cloud.md` before any mode; its rules add to this skill's.

Pick the mode from the argument. With no argument, ask which one.

## `setup` — prepare the current project

Run in the project root. Every step is safe to re-run.

1. **Detect the stack.** Read `package.json` (scripts, lockfile, test runner), `pyproject.toml`,
   `Makefile`, `.github/workflows/*`, and any `docker-compose*`/testcontainers use. Work out one
   command each for `{{SETUP_WORKTREE}}` (install deps in a fresh checkout), `{{TYPECHECK}}`,
   `{{LINT}}`, `{{ONE_TEST}}` (with a `<path>` placeholder), `{{FULL_SUITE}}`, and `{{REPO}}`
   (`gh repo view --json nameWithOwner --jq .nameWithOwner`). Ask the user in one round only for
   the ones still unknown; a command the project genuinely lacks becomes `none`.
2. **CLAUDE.md.** Fill `SKILL_DIR/templates/claude-md-section.md` and append it to `CLAUDE.md`
   (create the file when missing). The block sits between `<!-- feature-loop:start -->` and
   `<!-- feature-loop:end -->`; when those markers already exist, show the user the difference
   and replace only what sits between them. Every line outside the markers stays as it was.
3. **Copy files**, keeping any file the project already has (report it as kept):
   - `SKILL_DIR/templates/docs-agents/*.md` → `docs/agents/`, filling `{{REPO}}`
   - `SKILL_DIR/scripts/context-guard.py` → `scripts/claude/context-guard.py`
   - `SKILL_DIR/templates/settings.json` → `.claude/settings.json`: copy it when missing,
     otherwise merge in its two hook entries (skip one whose command already names
     `context-guard.py`) and its `attribution` key, keeping every other key.
   - add `.claude/worktrees/` to `.gitignore` when absent.
4. **Labels.** With a GitHub remote, create each label with
   `gh label create <name> --color <hex> --description "<text>" --force`:
   `feature` 5319e7 "A feature spec; parent of its tickets", `ticket` 1d76db "One slice of a
   feature, built by one implementer", `in-progress` fbca04 "An implementer is working on this",
   `ready-for-agent` 0e8a16, `ready-for-human` d93f0b, `needs-info` d876e3, `needs-triage`
   ededed, `wontfix` ffffff. Without a remote, skip this step and say so.
5. **Hand over.** Tell the user to type `/setup-matt-pocock-skills` once, and at its confirm step
   to keep the `docs/agents/` files just written (decisions live in `docs/decisions/`, or in
   `docs/design/decisions/` when the project has a design). When
   they say it finished, run `git status --short` and `git diff`, and report exactly what it
   added or changed.

Finish with a list of every file created, appended, merged or kept, and every label created.

## `plan` — Phase A, with the user present

Read [workflow.md](workflow.md), then run Phase A from its first step; its **Design check**
picks the slice route when the project has a `docs/design/` folder. If the user named a
hand-off file, or the newest `feature-loop-handoff-plan-*.md` in the temp dir matches the
current branch, read it and continue from its **Next action** instead.

## `orchestrate <feature issue>` — Phase B, unattended

Read [workflow.md](workflow.md) and [prompts/orchestrator.md](prompts/orchestrator.md), then
start at **Startup** for the given feature issue. When `HERDR_ENV=1`, also read
[herdr.md](herdr.md) and use its recipes to start, watch and prompt implementers.

## `gaps` — unchecked outside facts in `docs/design/`

Read [workflow.md](workflow.md) for the git and context rules, then run
[prompts/gaps.md](prompts/gaps.md) from step 1. Without a `docs/design/` folder, say so and stop.

## After the user merges the PR

When the user says the feature PR is merged, run Phase C of [workflow.md](workflow.md).
