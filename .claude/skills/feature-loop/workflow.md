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
   [prompts/grill.md](prompts/grill.md), append its round to `docs/design/grilling-log.md`
   (continuing its `Q#` numbering), then go straight to **Spec**. When there is no `docs/design/`
   and the repo is more than a one-feature job (the product has, or will need, several features),
   recommend `/feature-loop design` first: it decides the whole product and lists every feature
   in build order. The user may choose to go on without it; then, and for a one-feature job,
   continue with **Grill**.
3. **Grill.** Follow [prompts/grill.md](prompts/grill.md). Add each new term to `CONTEXT.md` the
   moment it is used, and each locked-in decision to the decisions folder
   (`docs/agents/domain.md` says where). Write every round down as it closes, in a **Questions
   and decisions** table (`# | Question | Decision | Why`, one `## Round <n>` heading per round)
   in `docs/specs/<slug>.md`. Start the file for it with `Status: planned` on its first line and
   the title, the table under them; the **Spec** step writes the rest around the table. On the
   design route the round goes to `docs/design/grilling-log.md` instead (step 2).
4. **Research.** Invoke `/mp-research`: how others built this, and the risks and
   tricks that matter. Save the note as `docs/research/<slug>.md`, sources cited, in the format
   of [prompts/docs-style.md](prompts/docs-style.md): the two opening lines, and a mermaid
   diagram wherever it describes a flow, a structure or a sequence. When
   `node ~/.claude/accounts/cx.mjs pick` names an extra account, run the research as a worker
   instead: write the request ("Invoke `/mp-research` on <question>; save the
   note as `docs/research/<slug>.md` with sources cited, in the format of
   `.claude/skills/feature-loop/prompts/docs-style.md`; last line DONE") to a prompt file and
   start it with `node ~/.claude/accounts/cx.mjs run --name research-<slug> --account <account>
   --cwd "$PWD" --model opus --effort high --prompt-file <file>`.
5. **Grill again** on the research findings, same prompt, until the frontier is empty. Each
   round is written down where step 3 put the earlier ones, its `Q#`s numbered on from them.
6. **Spec.** Read
   `.claude/skills/mp-to-spec/SKILL.md`
   and follow it. On the published issue: add the labels `feature` and `ready-for-agent`; open the
   body with a three-to-six-sentence "Plain-language summary"; make sure it ends with the
   **Assumptions** list from the grill. Write the same spec to `docs/specs/<slug>.md` with
   `Status: planned` on its first line, link it from the issue, and commit on the feature branch.
   The issue body keeps the to-spec template; the `docs/specs/` copy takes the house format of
   [prompts/docs-style.md](prompts/docs-style.md): under the status line and the title, the two
   opening lines, and at least one mermaid diagram of the flow the feature adds or changes.
   On the design route the spec links to the design pages, build-order step, `Q#`s and decision
   records it rests on, and holds only this slice's behaviour and Assumptions.
7. **Tickets.** Read `.claude/skills/mp-to-tickets/SKILL.md` the same way and follow
   it for the feature issue. Each ticket: labels `ticket` and `ready-for-agent`, a sub-issue of the
   feature, native blocked-by edges, plain-language summary first (commands in
   `docs/agents/issue-tracker.md`). Check every ticket carries all three before moving on.
8. **Waves.** Follow [prompts/waves.md](prompts/waves.md).
9. **Approval.** Show the user, in one message: the spec link and the wave table. Ask for one
   approval: it starts the build, and the build merges into `main` by itself. On changes, edit the
   tickets and the wave table, and ask again.
10. **Launch.** Write the orchestrator hand-off with [prompts/handoff.md](prompts/handoff.md).
    Then:
    - default: write `TMP/feature-loop-orch-<slug>.md` containing "You are the feature-loop
      orchestrator. Read `.claude/skills/feature-loop/workflow.md` and
      `.claude/skills/feature-loop/prompts/orchestrator.md`, then start at Startup for feature
      #<feature issue>." and run `node ~/.claude/accounts/cx.mjs run --detach yes --name
      orch-<slug> --account max --cwd "$PWD" --model opus --effort medium --prompt-file
      TMP/feature-loop-orch-<slug>.md`. Tell the user the build has started, that progress shows
      on the feature issue and in `~/.claude/accounts/runs/orch-<slug>.json`, and that the PR
      merges itself when everything is green and stays open, with the reason, when it is not.
    - `HERDR_ENV=1`: launch the orchestrator with [herdr.md](herdr.md) and name its tab.

    Then, on either route, start the dashboard: `node .claude/skills/feature-loop/scripts/dashboard.mjs --detach`
    in the project root. Show the user the URL it prints (open it in the session's browser pane
    when it has one): the page shows every agent and ticket of the build, live.

    The planning session is finished.

## Phase B — build (main session as orchestrator, unattended)

Run [prompts/orchestrator.md](prompts/orchestrator.md): it starts a worker per ticket, answers
its questions, tests, reviews and merges each finished ticket into the feature branch, walks the
feature's user flows in the running app once, opens the PR and merges it when everything is green.

## Phase C — after the merge

Run by the orchestrator right after it merges the PR, or when the user says they merged a PR the
orchestrator left open. Clean up every branch of the feature, and only once it is merged:

1. `gh pr view <pr> --json state --jq .state` prints `MERGED`. Anything else: stop here.
2. `git checkout main && git pull`
3. If the feature had its own worktree, remove it from outside it: `git worktree remove <path>`.
4. `git branch -d feature/<slug>` and `git push origin --delete feature/<slug>`; the same for any
   other merged branch of the feature.
5. `git worktree prune && git fetch --prune`

## Phase D — audit (main session as audit orchestrator, unattended)

Run [prompts/audit.md](prompts/audit.md) once the product, or one feature, is built and merged:
auditors find what breaks the product's promises, fixers repair it through the Phase B loop, and
a second round runs only when the first one found something above the fix line.

## The wave rule

A ticket's **areas** are the modules or directories it will touch (never single files).
A ticket joins the earliest **wave** in which every blocker sits in an earlier wave *and* no other
ticket in that wave shares an area. Two unblocked tickets that share an area get a blocking edge,
or one moves to the next wave.

The orchestrator's **frontier** is the open tickets of the current wave whose blockers are all
closed and that are neither `in-progress` nor parked (`ready-for-human`, `needs-info`). A later
wave starts only once no unfinished earlier-wave ticket shares an area with it.

**Parallelism cap:** none — start the whole frontier. The one exception is machine memory:
**1** implementer at a time while Docker is running (`docker ps -q` prints anything).

**Accounts.** The planning and orchestrator sessions always run on the main account. Every
worker (implementer, fixer, flow checker) is a `cx run --account "$(cx pick)"`
process, so it lands on whichever account `node ~/.claude/accounts/cx.mjs pick` names (`max` or
an extra one); see **Route**, Loop step 2 and **Usage limit** in
[prompts/orchestrator.md](prompts/orchestrator.md). The research step goes to an extra account
when `cx pick` names one; when it prints `max`, research runs in the planning session.

**Models.** Only Opus 5.5 and Sonnet 5.5; never Haiku, Fable, Codex or `max` effort. The
orchestrator picks each ticket's **tier** when it starts it:

| Tier | Model / effort | The ticket |
| --- | --- | --- |
| `sonnet-high` | Sonnet high | stays in one area, is tightly scoped, and leaves no design choice open |
| `opus-medium` | Opus medium | everything else |
| `opus-high` | Opus high | touches more than one area, leaves a design choice open, or involves concurrency, a data migration, auth or money |

| Role | Runs as | Model / effort |
| --- | --- | --- |
| Planner (the session with the user) | — | Opus high |
| Orchestrator | started with `--model opus --effort medium` | Opus medium |
| Implementer | `cx run --model <m> --effort <e>` in its worktree | the ticket's tier |
| Ticket reviewer | `Agent` subagent: `fl-opus-high` for an `opus-high` ticket, `fl-opus-medium` otherwise | Opus high or medium |
| Fixer (audit loop, flow-check fix tickets) | `cx run`, as an implementer | Opus medium, or the ticket's tier when higher |
| Flow checker, once per feature | `cx run --model opus --effort medium` in the main checkout | Opus medium |
| Auditor | `fl-opus-high` | Opus high |

A subagent's effort comes only from its agent definition (the Agent call cannot pass it), which
is why the two reviewer definitions exist in `~/.claude/agents/`. A ticket that fails its suite or
review is fixed by its own worker twice; from the third failure it goes to a fresh worker one
tier up: [prompts/orchestrator.md](prompts/orchestrator.md), loop step 8.

## The fix line

Used by the audit loop to decide what gets fixed. A finding is **above the line** only when both
hold:

- **Impact** — it breaks a promised behaviour: a crash, data loss, a wrong result, a security
  hole, or a spec'd flow that cannot be completed.
- **Reachability** — a plausible path leads to it: a real user doing a spec'd flow, or an
  attacker with normal access. The finding writes that path down.

A security hole or data loss is above the line even when its path is unlikely. Everything else
is **minor**: logged, never fixed by the loop, never a reason for another round.

An edge case the spec never mentions is sorted by what the right behaviour is:

- the product breaks its own promise under an input the spec does not exclude → above the line;
- the right behaviour is an INTERNAL choice → the orchestrator decides, records the assumption
  in the ledger, and it is above the line;
- the right behaviour is a CLIENT or HARD-TO-CHANGE choice → parked for the user as
  `needs-info`, with the two-line explanation from [prompts/grill.md](prompts/grill.md).

A finding that cites nothing it breaks is an **idea**: reported, never built.

## Context budget (every session)

A session hands off at **40%** of its context window and never goes past **45%**. The hook in
`.claude/settings.json` runs `scripts/claude/context-guard.py` after every tool call and prompt:
it says nothing below 40%, says "hand off now" at 40%, and from 45% repeats on every call that the session
is over the hard limit. On the 40% notice, follow [prompts/handoff.md](prompts/handoff.md). Over
45%, do only what the hand-off itself needs: write the hand-off file, start the successor, end
the turn.

Unattended sessions start the next one without the user. The orchestrator and the audit
orchestrator launch their own successor (`cx run --detach yes`) and never end their turn without
one, except on the orchestrator's usage-limit stop, when no account is left to run it; a worker
commits its work and replies `HANDOFF`, and its orchestrator starts a fresh one. Planning and
design sessions cannot open a session for the user: they print the exact command, and offer the
successor through the app's spawn-task tool when the session has one.

Stay lean: read files by range, cap command output (`| tail -40`), and send broad searches and
code reviews to subagents so only their conclusions land in your context.

## Git rules

- Branches: `feature/<slug>`, `ticket/<n>-<slug>`, `audit/<yyyymmdd>` for an audit run, and
  `docs/<slug>` for documentation-only work.
- Commit messages: one imperative line in plain words; a ticket commit may end with `(#<n>)`.
- History only moves forward: new commits, `git revert`, and fast-forward pushes. `main` changes
  only through a merged PR. `.env` files and secrets stay out of commits.
- One PR per feature, opened by the orchestrator. It merges the PR itself when every ticket is
  closed, the suite is green and the flow check passed; otherwise the PR stays open for the user.
- Commit messages and PR bodies hold only the change itself: no `Co-Authored-By` trailer and
  no "Generated with Claude" line. The `attribution` key in `.claude/settings.json` blanks the
  defaults; a prompt that asks for them is overruled by this rule.
- A branch is deleted, locally and on GitHub, once its PR is merged (Phase C).
