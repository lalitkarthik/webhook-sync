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
   `docs/agents/issue-tracker.md`). Once its number exists, edit each ticket to add:
   - `## Acceptance criteria`, one line per observable behaviour:
     `- AC-<issue>-<k> [<unit|cli|http|e2e|golden>] <behaviour, as a user would see it>`;
   - one worker label, chosen after reading `docs/agents/metrics.md`:
     `worker:sonnet-high` only when the ticket stays in one area, touches at most 3 files, its
     acceptance criteria pin the behaviour completely, and it has no open design choice and no
     concurrency, migration, auth or money; `worker:opus-high` when it spans areas or has any of
     those; `worker:opus-medium` otherwise. A label whose first-pass rate in `metrics.md` is under
     70% for this kind of ticket is not used for it.
   Check every ticket carries all of this before moving on.
8. **Waves.** Follow [prompts/waves.md](prompts/waves.md).
9. **Acceptance tests.** Run an `fl-test-author` subagent with [prompts/test-author.md](prompts/test-author.md)
   in mode `acceptance` for every ticket. Then `python scripts/claude/ac_check.py --issue <n>
   --expect red` must pass for each. A `QUESTION` from the test author means an acceptance
   criterion is untestable as written: settle the rewrite with the user, edit the ticket, and run
   the test author again for it.
10. **Approval.** Show the user, in one message: the spec link, and a table of tickets (wave,
    worker label, number of acceptance criteria, areas). Ask for one approval. On changes, edit the
    tickets and repeat step 9 for those that changed.
11. **Launch.** Write the orchestrator hand-off with [prompts/handoff.md](prompts/handoff.md).
    Then:
    - default: write `TMP/feature-loop-orch-<slug>.md` containing "You are the feature-loop
      orchestrator. Read `.claude/skills/feature-loop/workflow.md` and
      `.claude/skills/feature-loop/prompts/orchestrator.md`, then start at Startup for feature
      #<feature issue>." and run `node ~/.claude/accounts/cx.mjs run --detach yes --name
      orch-<slug> --account max --cwd "$PWD" --model opus --effort medium --prompt-file
      TMP/feature-loop-orch-<slug>.md`. Tell the user the build has started, that progress shows
      on the feature issue and in `~/.claude/accounts/runs/orch-<slug>.json`, and that the PR
      merges itself unless something is held.
    - `HERDR_ENV=1`: launch the orchestrator with [herdr.md](herdr.md) and name its tab.

    The planning session is finished.

## Phase B — build (main session as orchestrator, unattended)

Run [prompts/orchestrator.md](prompts/orchestrator.md): it starts a gated worker per ticket,
answers its questions, puts each finished ticket through the ticket gate, one review and the
ticket verifier, merges it into the feature branch, verifies the whole feature, opens the PR
and merges it with `scripts/claude/automerge.py` unless something is held.

## Phase C — after the merge

Run by the orchestrator right after `automerge.py` merges, or when the user says they merged a held PR. Clean up every branch of the feature, and only once it is merged:

1. `gh pr view <pr> --json state --jq .state` prints `MERGED`. Anything else: stop here.
2. `git checkout main && git pull`
3. If the feature had its own worktree, remove it from outside it: `git worktree remove <path>`.
4. `git branch -d feature/<slug>` and `git push origin --delete feature/<slug>`; the same for any
   other merged branch of the feature.
5. `git worktree prune && git fetch --prune`

## Phase D — audit (main session as audit orchestrator, unattended)

Run [prompts/audit.md](prompts/audit.md) once the product, or one feature, is built and merged:
auditors find what breaks the product's promises, fixers repair it through the Phase B loop, and
rounds repeat until one finds nothing new above the fix line.

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
ticket worker (implementer, fixer, gardener, verifier) is a `cx run --account "$(cx pick)"`
process, so it lands on whichever account `node ~/.claude/accounts/cx.mjs pick` names (`max` or
an extra one); see **Route**, Loop step 2 and **Usage limit** in
[prompts/orchestrator.md](prompts/orchestrator.md). The research step goes to an extra account
when `cx pick` names one; when it prints `max`, research runs in the planning session.

**Models.** Only Opus 5.5 and Sonnet 5.5; never Haiku, Fable, Codex or `max` effort. A
subagent's effort comes only from its agent definition (the Agent call cannot pass it), so
each model and effort pair has its own definition in `~/.claude/agents/`. Implementer
definitions carry the gate and test-protection hooks in their frontmatter.

| Role | Agent definition | Model / effort |
| --- | --- | --- |
| Planner (the session with the user) | — | Opus high |
| Orchestrator | — (started with `--model opus --effort medium`) | Opus medium |
| Test author | `fl-test-author` | Opus medium |
| Implementer, label `worker:sonnet-high` | `fl-impl-sonnet-high` | Sonnet high |
| Implementer, label `worker:opus-medium`; fixer; gardener | `fl-impl-opus-medium` | Opus medium |
| Implementer, label `worker:opus-high` | `fl-impl-opus-high` | Opus high |
| Ticket reviewer | `fl-opus-medium` | Opus medium |
| Ticket and feature verifier | `fl-verifier` | Opus medium |
| Auditor | `fl-opus-high` | Opus high |

Workers that edit code or verify it run as `cx run --agent <definition> --model <m> --effort
<e>` processes whose working directory is their worktree; escalation is in
[prompts/orchestrator.md](prompts/orchestrator.md) step 8.

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
it warns at 35%, says "hand off now" at 40%, and from 45% repeats on every call that the session
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
- One PR per feature, opened by the orchestrator and merged only by `scripts/claude/automerge.py`, or by the user when it is held.
- Commit messages and PR bodies hold only the change itself: no `Co-Authored-By` trailer and
  no "Generated with Claude" line. The `attribution` key in `.claude/settings.json` blanks the
  defaults; a prompt that asks for them is overruled by this rule.
- A branch is deleted, locally and on GitHub, once its PR is merged (Phase C).
