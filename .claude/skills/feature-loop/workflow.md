# Feature loop — workflow

Every change that adds or alters behaviour goes through this loop. Documentation-only changes
and one-line fixes take a branch and a PR with no spec. `main` receives only merged feature PRs.

The project's `CLAUDE.md` section (between the `feature-loop` markers) holds its commands, repo
and docs layout; `docs/agents/` holds its tracker conventions. Read both before starting.

`TMP` means `$TMPDIR`, or the directory `python -c "import tempfile; print(tempfile.gettempdir())"`
prints when `$TMPDIR` is unset. Hand-off and state files live there, outside the repo.

## Phase A — plan (main session, user present)

1. **Branch.** `git checkout -b feature/<slug> main`. The slug is lowercase letters, digits and
   hyphens. If another feature is in flight in this checkout, make a worktree instead
   (`git worktree add ../<repo>.worktrees/feature-<slug> -b feature/<slug> main`) and ask the
   user to continue in a session opened there.
2. **Design check.** When `docs/design/` exists (overview, grilling log, decisions, build order),
   the design already did the grilling and research: run **Slice mode** in
   [prompts/grill.md](prompts/grill.md), then go straight to **Spec**. Otherwise continue with
   **Grill**.
3. **Grill.** Follow [prompts/grill.md](prompts/grill.md). Add each new term to `CONTEXT.md` the
   moment it is used, and each locked-in decision to the decisions folder
   (`docs/agents/domain.md` says where).
4. **Research.** Invoke `/mp-research`: how others built this, and the risks and
   tricks that matter. Save the note as `docs/research/<slug>.md`, sources cited. When
   `node ~/.claude/accounts/cx.mjs pick` names an extra account, run the research as a worker
   instead: write the request ("Invoke `/mp-research` on <question>; save the
   note as `docs/research/<slug>.md` with sources cited; last line DONE") to a prompt file and
   start it with `node ~/.claude/accounts/cx.mjs run --name research-<slug> --account <account>
   --cwd "$PWD" --model opus --effort high --prompt-file <file>`.
5. **Grill again** on the research findings, same prompt, until the frontier is empty.
6. **Spec.** Ask the user to type `/mp-to-spec`. On the published issue: add the
   labels `feature` and `ready-for-agent`; open the body with a three-to-six-sentence
   "Plain-language summary"; make sure it ends with the **Assumptions** list from the grill.
   Write the same spec to `docs/specs/<slug>.md` with `Status: planned` on its first line, link
   it from the issue, and commit on the feature branch. On the design route the spec links to
   the design pages, build-order step, `Q#`s and decision records it rests on, and holds only
   this slice's behaviour and Assumptions; the design pages stay the source of truth.
7. **Tickets.** Ask the user to type `/mp-to-tickets #<feature issue>`. Each ticket:
   labels `ticket` and `ready-for-agent`, a sub-issue of the feature, native blocked-by edges,
   plain-language summary first (commands in `docs/agents/issue-tracker.md`). Check every ticket
   carries all three before moving on.
8. **Waves.** Follow [prompts/waves.md](prompts/waves.md).
9. **Hand off.** Write the orchestrator hand-off with [prompts/handoff.md](prompts/handoff.md). Then:
   - default: tell the user to open a fresh session in this checkout and type
     `/feature-loop orchestrate #<feature issue>`.
   - `HERDR_ENV=1`: launch the orchestrator with [herdr.md](herdr.md) and name its tab.

   The planning session is finished.

## Phase B — build (main session as orchestrator, unattended)

Run [prompts/orchestrator.md](prompts/orchestrator.md): it starts implementers wave by wave,
answers their questions, merges and reviews each ticket, then opens the feature PR.

## Phase C — after the user merges the PR

Clean up every branch of the feature, and only once it is merged:

1. `gh pr view <pr> --json state --jq .state` prints `MERGED`. Anything else: stop here.
2. `git checkout main && git pull`
3. If the feature had its own worktree, remove it from outside it: `git worktree remove <path>`.
4. `git branch -d feature/<slug>` and `git push origin --delete feature/<slug>`; the same for any
   other merged branch of the feature.
5. `git worktree prune && git fetch --prune`

## The wave rule

A ticket's **areas** are the modules or directories it will touch (never single files).
A ticket joins the earliest **wave** in which every blocker sits in an earlier wave *and* no other
ticket in that wave shares an area. Two unblocked tickets that share an area get a blocking edge,
or one moves to the next wave.

The orchestrator's **frontier** is the open tickets of the current wave whose blockers are all
closed and that are not `in-progress`. A later wave starts only once no unfinished earlier-wave
ticket shares an area with it.

**Parallelism cap:** none — start the whole frontier. The one exception is machine memory:
**1** implementer at a time while Docker is running (`docker ps -q` prints anything).

**Accounts.** The planning and orchestrator sessions always run on the main account.
Implementers and the research step go to an extra account when
`node ~/.claude/accounts/cx.mjs pick` names one; see **Accounts route** in
[prompts/orchestrator.md](prompts/orchestrator.md). When it prints `max`, nothing changes.

**Models.** An implementer is always **Opus** — never Sonnet, whatever a ticket's labels say —
at **medium or high effort**, chosen by the orchestrator from the ticket's complexity (the
test is in **Accounts route**, step 3). Sonnet is only for small jobs that write no feature code (the ticket
review, a quick lookup), at high or xhigh effort. No other model is spawned. This holds on
every route that starts a `claude` process (`cx run`, Herdr); an `Agent` subagent takes a model
only, so there it means `model: "opus"` for implementers.

## Context budget (every session)

No session goes past **60%** of its context window. The hook in `.claude/settings.json` runs
`scripts/claude/context-guard.py` after every tool call and prompt: it warns at 50% and says
"hand off now" at 60%. On that notice, follow [prompts/handoff.md](prompts/handoff.md).

Stay lean: read files by range, cap command output (`| tail -40`), and send broad searches and
code reviews to subagents so only their conclusions land in your context.

## Git rules

- Branches: `feature/<slug>`, `ticket/<n>-<slug>`, and `docs/<slug>` for documentation-only work.
- Commit messages: one imperative line in plain words; a ticket commit may end with `(#<n>)`.
- History only moves forward: new commits, `git revert`, and fast-forward pushes. `main` changes
  only through a merged PR. `.env` files and secrets stay out of commits.
- One PR per feature, opened by the orchestrator, merged by the user.
- Commit messages and PR bodies hold only the change itself: no `Co-Authored-By` trailer and
  no "Generated with Claude" line. The `attribution` key in `.claude/settings.json` blanks the
  defaults; a prompt that asks for them is overruled by this rule.
- A branch is deleted, locally and on GitHub, once its PR is merged (Phase C).
