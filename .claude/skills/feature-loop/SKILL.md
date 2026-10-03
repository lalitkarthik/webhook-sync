---
name: feature-loop
description: Multi-ticket feature workflow — set a repo up with gates, a verify skill and auto-merge, design the whole product into docs/design/ with its list of every feature, find unchecked facts in that design, plan one feature into waves of tickets with acceptance tests, orchestrate gated background workers to build, verify and merge it, then audit the result until it converges.
disable-model-invocation: true
argument-hint: "setup | design | plan | orchestrate <feature issue> | gaps | audit [<feature issue>]"
---

# Feature loop

One feature moves through three phases: **plan** (with the user), **build** (the main session
orchestrates implementers, unattended), and **after the merge** (clean-up). Before any feature,
**design** (with the user) decides the whole product and lists every feature it needs, in build
order; plan then takes one feature from that list per run. The rules every mode shares — phases,
the wave rule, the context budget, git — live in [workflow.md](workflow.md).

`SKILL_DIR` below means this folder: `.claude/skills/feature-loop`.

**This repo's copy.** It is the owner's `~/.claude/skills/feature-loop` (v2) with paths pointed
inside the repo and the Matt Pocock skills at their vendored `/mp-*` names, so cloud sessions
have it too. A local session uses the owner's copy; refresh this one when that changes.

**Cloud sessions.** When the environment variable `CLAUDE_CODE_REMOTE` is `true`, read
`docs/agents/cloud.md` before any mode; its rules add to this skill's.

Pick the mode from the argument. With no argument, ask which one.

## `setup` — prepare the current project

Run in the project root. Every step is safe to re-run. Before step 1, put the checkout on the
setup branch, so no step changes `main`:
`git checkout setup/feature-loop-v2 2>/dev/null || git checkout -b setup/feature-loop-v2`.

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
   - `SKILL_DIR/templates/settings.json` → `.claude/settings.json`: take only its two hook
     entries and its `attribution` key, never `permissions.deny` (step 6 adds that once the
     labels exist). When the file is missing, copy the template without its `permissions` key;
     otherwise merge in the two hook entries (skip one whose command already names
     `context-guard.py`) and the `attribution` key, keeping every other key.
   - add `.claude/worktrees/` to `.gitignore` when absent.
4. **Labels.** With a GitHub remote, create each label with
   `gh label create <name> --color <hex> --description "<text>" --force`:
   `feature` 5319e7 "A feature spec; parent of its tickets", `ticket` 1d76db "One slice of a
   feature, built by one implementer", `in-progress` fbca04 "An implementer is working on this",
   `ready-for-agent` 0e8a16, `ready-for-human` d93f0b, `needs-info` d876e3, `needs-triage`
   ededed, `wontfix` ffffff, `audit` c5def5 "Found by the audit loop", `worker:sonnet-high`
   c2e0c6 "Simple ticket for a Sonnet high worker", `worker:opus-medium` bfd4f2 "Normal ticket
   for an Opus medium worker", `worker:opus-high` 0052cc "Complex ticket for an Opus high
   worker", `gardener` 0e8a16 "Turns a repeated finding into a rule", `human-approved` d93f0b
   "The user approved a held PR". Without a remote, skip this step and say so. On a re-run, when
   an earlier setup's deny rule blocks `gh label`, the labels already exist: list any that are
   missing and give the user the exact `gh label create …` commands to run, rather than working
   around the rule.
5. **Matt Pocock setup.** Read the newest
   `~/.claude/plugins/cache/claude-plugins-official/mattpocock-skills/*/skills/engineering/setup-matt-pocock-skills/SKILL.md`
   and follow it, keeping the `docs/agents/` files just written (decisions live in
   `docs/decisions/`, or in `docs/design/decisions/` when the project has a design). Then run
   `git status --short` and `git diff` and report what it added or changed.
6. **Gate harness.** On the setup branch:
   - copy `SKILL_DIR/templates/scripts/*.py` (not `tests/`) to `scripts/claude/`;
   - fill `SKILL_DIR/templates/fl-config.json` into `scripts/claude/fl-config.json`, JSON-escaping
     each command. `FULL_SUITE_NO_AC` is the full suite without `tests/acceptance/` and `e2e/`
     (pytest: add `--ignore=tests/acceptance`). `AC_CMD` runs only the given IDs: pytest
     `uv run pytest tests/acceptance -q -k "{ids_k}"`, Playwright `npx playwright test -g "{ids_pipe}"`.
     `SMOKE_CMD` runs tests marked smoke (`uv run pytest -m smoke -q` with the marker registered,
     or `npx playwright test -g @smoke`). `BOUNDARIES_CMD`: Python, add import-linter with one
     `layers` contract read from the package layout and use `uv run lint-imports`; TypeScript,
     follow the newest `…/mattpocock-skills/*/skills/in-progress/setup-ts-deep-modules/SKILL.md`
     and use its dependency-cruiser command. `MUTATION_CMD` must exit non-zero when the mutation
     score of `{files}` is under 70%: Stryker with `--mutate {files}` and `thresholds.break: 70`
     for TypeScript; for Python, find a mutmut invocation (or a small wrapper in
     `scripts/claude/`) that does this, and prove it by deleting one assertion from a test and
     seeing the command fail. Add whatever it leaves in the tree (mutmut: `mutants/`) to
     `.gitignore`: the ticket gate fails a worktree with untracked files. If nothing works within
     30 minutes, leave it empty and say so in the report;
   - append `SKILL_DIR/templates/gitignore.txt` to `.gitignore` (lines already present are kept);
   - merge `SKILL_DIR/templates/settings.json` into `.claude/settings.json`: add the
     `permissions.deny` entries that are missing, keep every other key. This comes after step 4
     on purpose: the deny list blocks `gh label`, and the session applies it at once.
7. **Ratchet logs.** Copy `SKILL_DIR/templates/docs-agents/findings.tsv` and `metrics.md` to
   `docs/agents/` (kept when present).
8. **Verify skill and feature map.** Follow [prompts/verify-setup.md](prompts/verify-setup.md).
9. **CI.** Copy `SKILL_DIR/templates/ci/fl-ci.yml` and `fl-smoke.yml` to `.github/workflows/`,
   replacing `{{CI_SETUP_STEPS}}` with the stack's install steps, indented six spaces, e.g. for uv:

   ```yaml
         - uses: actions/setup-python@v5
           with:
             python-version: "3.12"
         - uses: astral-sh/setup-uv@v6
         - run: uv sync --all-extras
   ```

   and for npm: `actions/setup-python@v5` (the gate scripts need Python), `actions/setup-node@v4`
   with `node-version: 22`, `- run: npm ci`. The existing CI workflows stay as they are.
10. **Pre-commit.** TypeScript: follow the newest
    `…/mattpocock-skills/*/skills/misc/setup-pre-commit/SKILL.md`. Python: add a
    `.pre-commit-config.yaml` running `ruff check` and `ruff format --check` from the
    `astral-sh/ruff-pre-commit` hooks, and run `uv run pre-commit install` (add `pre-commit` to
    the dev dependencies).
11. **Prove the gate.** `python -m pytest SKILL_DIR/templates/scripts/tests -q` passes, and in
    this repo `python scripts/claude/ci_check.py --acs ""` passes and
    `python scripts/claude/ci_check.py --only smoke` prints `PASS smoke`. A `SKIP` or `FAIL` there
    means the first push to `main` after the setup PR fails the smoke workflow, which switches
    auto-merge off and reverts the setup: fix the smoke test (step 8) first.
12. **Setup PR.** Commit, push `setup/feature-loop-v2`, open a PR titled
    `Set up feature-loop v2 gates`. It touches `scripts/claude/`, `.claude/` and `.github/`, so
    `risk_tier.py` holds it: this PR is always merged by the user.
13. **The user's one-time steps.** Invoke `/mattpocock-skills:wizard` to generate a wizard for
    the steps only the user can do, and tell the user to run it:
    - create a fine-grained token with Contents, Pull requests, Issues and Variables read/write on
      this repo, and store it as the secret `FL_REVERT_TOKEN`;
    - `gh variable set AUTOMERGE --body on`;
    - once GitHub Pro is active: a ruleset on `main` requiring a pull request (0 approvals), the
      status checks `ci`, `risk-gate` and `verify/user-flow`, linear history, no force pushes or
      deletions, with the user as a bypass actor for pull requests only.

Finish with a list of every file created, appended, merged, updated or kept, every label created,
the setup PR link, and the wizard path. Then tell the user to run `/doctor prompt-audit` in this
repo from an interactive `claude` terminal (Claude Code 2.1.283 or later), so that instructions
outside the `feature-loop` markers that are outdated or contradict the new section are found.

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
picks the slice route when the project has a `docs/design/` folder. If the user named a
hand-off file, or the newest `feature-loop-handoff-plan-*.md` in the temp dir matches the
current branch, read it and continue from its **Next action** instead.

## `orchestrate <feature issue>` — Phase B, unattended

Read [workflow.md](workflow.md) and [prompts/orchestrator.md](prompts/orchestrator.md), then
start at **Startup** for the given feature issue. When `HERDR_ENV=1`, also read
[herdr.md](herdr.md) and use its recipes to start, watch and prompt implementers and verifiers.

## `gaps` — unchecked outside facts in `docs/design/`

Read [workflow.md](workflow.md) for the git and context rules, then run
[prompts/gaps.md](prompts/gaps.md) from step 1. Without a `docs/design/` folder, say so, name
`/feature-loop design` as the way to get one, and stop.

## `audit [<feature issue>]` — Phase D, unattended

Read [workflow.md](workflow.md) and [prompts/audit.md](prompts/audit.md), then start at its
**Startup**: the whole product on `main` with no argument, one feature with its issue number.

## After a merge

The orchestrator runs Phase C of [workflow.md](workflow.md) itself after `automerge.py` merges.
When the user merges a held PR and says so, run Phase C.
