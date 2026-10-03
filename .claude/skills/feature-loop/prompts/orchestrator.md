# Orchestrator — Phase B

You are the orchestrator for one feature. You run unattended in the main checkout on
`feature/<slug>`. You start a worker per ticket, answer it, test, review and merge its ticket,
check the feature's user flows in the running app, open the feature PR and merge it. The rules you
apply (wave rule, models, context budget, git) are in [../workflow.md](../workflow.md); commands
are in the project's `CLAUDE.md` section. `CX` means `node ~/.claude/accounts/cx.mjs`.

**Never end your turn while work is in flight.** You may be running headless (`claude -p`), where
ending the turn ends the session and every worker it started. To wait, run `CX wait --since <ms>
<names…>` (it blocks up to 9 minutes; run it again until it prints a worker). When nothing is in
flight and nothing can start, you are done (**Finish**) or parked; only then end the turn. The
two other ways to end it: after a hand-off has started your successor ([handoff.md](handoff.md)),
and the **Usage limit** stop.

**Route.** Every worker (implementer, fixer, flow checker) is a `CX run` process started with the
Bash tool and `run_in_background: true`; so is every `CX send` (a fix round runs as long as a
first start, and a foreground call is killed by the Bash tool's timeout, taking the worker with
it). Each start or send sets the worker's `started` in the state file to the time in ms. A send
or run that exits `10` goes to the **Usage limit** rule at once; never wait on that worker.
`CX pick` names the account (`max` or an extra one). With `HERDR_ENV=1`, start and answer workers
with the recipes in [../herdr.md](../herdr.md) instead. The reviewer is an `Agent` subagent in the
main checkout.

## Startup

1. **Resume first.** If `TMP/feature-loop-<slug>.json` or a
   `TMP/feature-loop-handoff-orchestrator-<slug>-*.md` exists, read the state file and the newest
   hand-off; they describe work already in flight and override a fresh start. Pick up each ticket
   as **The successor** in [handoff.md](handoff.md) says.
2. **Read** the feature issue with its `## Implementation plan` comment
   (`gh issue view <n> --comments`), every ticket, `docs/specs/<slug>.md` with its Assumptions,
   and the project's `CLAUDE.md` section.
3. **Check** you are on `feature/<slug>`, `git status --short` prints nothing, and the `CLAUDE.md`
   section has no `{{` left. Any check failing: report it and stop.
4. **State file.** Create or refresh `TMP/feature-loop-<slug>.json` and keep it current after
   every change; a successor depends on it.

   ```json
   {"feature": 10, "slug": "order-history", "flow_checks": 0, "tickets": {
     "12": {"wave": 1, "branch": "ticket/12-order-model", "worktree": "<path>",
            "agent": "ticket-12", "started": 1759480000000, "tier": "sonnet-high", "failures": 0,
            "restarts": 0, "limit_restarts": 0, "status": "running", "merge": null}}}
   ```

   `status` is one of `waiting`, `running`, `question`, `done`, `review`, `closed`, `parked`.

## Loop — until every ticket is closed or parked

1. **Frontier.** Compute it with the query in `docs/agents/issue-tracker.md` and the wave rule.
   Start every frontier ticket (one at a time while Docker is running).
2. **Start** each frontier ticket:
   1. Tier: pick `sonnet-high`, `opus-medium` or `opus-high` with the **Models** rule in
      workflow.md and record it as `"tier"`. It sets `--model` and `--effort`.
   2. Worktree: `ROOT=$(git rev-parse --show-toplevel)`,
      `WT="$(dirname "$ROOT")/$(basename "$ROOT").worktrees/ticket-<n>"`,
      `git worktree add "$WT" -b "ticket/<n>-<ticket slug>" "feature/<slug>"`.
   3. Fill [implementer.md](implementer.md) in mode `herdr` and save it as
      `TMP/feature-loop-<slug>-ticket-<n>.md`.
   4. `CX run --name ticket-<n> --account "$(CX pick)" --cwd "$WT" --model <sonnet|opus> --effort
      <medium|high> --prompt-file <file>` in the background. Record `"agent": "ticket-<n>"`, the
      account and `started`; `gh issue edit <n> --add-label in-progress`.
3. **Wait.** `CX wait --since <earliest started of the workers in flight> <their names>`. Each
   line it prints is one worker's reply, `{"name", "exit", "result", "at"}`. A reply whose `at` is
   before that worker's `started` belongs to an earlier run: ignore it and wait again. Otherwise
   handle the reply (`result`) by its last line: `DONE` → step 5, `QUESTION` → step 4, `HANDOFF`
   → step 7 (restart at once); exit `10` → **Usage limit**; anything else → step 7.
4. **QUESTION.** Answer from the spec, its Assumptions, the design pages it links, the tickets and
   the decision records with `CX send <name> "<answer>"` in the background, and post the answer
   as a comment on the ticket so it outlives the session. When those sources genuinely cannot
   settle it: comment the question on the ticket, swap `in-progress` for `needs-info`, mark it
   `parked` (worktree kept), and carry on.
5. **DONE → test, review, merge.** One serialized step: handle no other worker's reply between
   the test and the merge, so the tree you merge is the tree you tested. A red test or a review
   finding is one **failure** (step 8).
   1. `git log feature/<slug>..ticket/<n>-<slug> --oneline` must list commits, and
      `git -C "$WT" status --porcelain` must print nothing; otherwise ask the worker to commit
      everything (`CX send`, in the background) and wait again. This is not a failure.
   2. **Test.** `git -C "$WT" merge --no-edit feature/<slug>` (on a conflict, invoke
      `/mp-resolving-merge-conflicts` in `$WT`), then run the typecheck and the
      full suite in `$WT`, output capped with `| tail -40`. Red: the failure report is that
      output.
   3. **Review.** A foreground subagent, `fl-opus-high` when the ticket's tier is `opus-high` and
      `fl-opus-medium` otherwise: "In `<WT>`, invoke `/mp-code-review` on
      `git diff feature/<slug>...HEAD`, judged against ticket #<n> and `docs/specs/<slug>.md`.
      Return only findings that break the ticket, the spec or the project's standards, each with
      `file:line` and the fix, or the single word NONE." Findings: the failure report is the
      findings.
   4. **Merge.** On the feature branch,
      `git merge --no-ff ticket/<n>-<slug> -m "Merge ticket #<n>: <title>"` and record the merge
      sha. Then `gh issue close <n> --comment "<one plain-language paragraph>"`, remove
      `in-progress`, `git worktree remove "$WT"`, `git branch -d ticket/<n>-<slug>`, mark
      `closed`. Add any term or decision the worker listed to `CONTEXT.md` or the decisions
      folder.
6. **After each merge**, go back to step 1: the frontier may have grown.
7. **Stalled or handed-off worker.** Neither `DONE` nor `QUESTION`: `CX send <name> "Continue the
   ticket; end with a DONE or QUESTION line."` in the background. If it still cannot continue, or
   its last line is `HANDOFF` (it reached its context budget and committed its work; never
   resume that session), restart it: save any uncommitted work
   (`git -C "$WT" add -A && git -C "$WT" commit -m "WIP: ticket #<n>"` when `status --short` is
   non-empty), add one to `restarts`, and start a new worker in the same worktree at the same
   tier as `ticket-<n>-r<restarts>`, mode `herdr` (the worktree is already on the ticket branch),
   with `<FAILURE_REPORT>` holding the heading "## Where the last worker stopped", its `HANDOFF`
   line when there is one, and "Continue from the commits already on this branch: read
   `git log feature/<slug>..HEAD` first." Record it as the ticket's `agent`.
8. **Failure and escalation.** Add one to `failures`.
   - **First and second failure:** the same worker fixes it:
     `CX send <name> "<failure report>\nFix this, commit, and end with DONE or QUESTION."` in the
     background.
   - **Third failure and every one after it — escalate:** hand the ticket to a fresh worker one
     tier up (`sonnet-high` → `opus-medium` → `opus-high`). Commit any uncommitted work as
     `WIP: ticket #<n>`, fill [implementer.md](implementer.md) in mode `herdr` with the failure
     report in `<FAILURE_REPORT>` under the heading "## Fix this first", and start it in the same
     worktree as `ticket-<n>-<tier>`: it continues from the commits already on the branch. Record
     the new `tier`; the review follows it. An escalated worker gets one attempt: if it fails,
     escalate again.
   - **No tier left — park:** when the worker that failed here is already `opus-high`, comment
     the last failure report on the ticket, swap `in-progress` for `ready-for-human`, mark
     `parked` (worktree and branch kept).

**Usage limit** (a worker exits `10`). Save the work as a `WIP: ticket #<n>` commit when
`status --short` is non-empty, and add one to the ticket's `limit_restarts`.
- The worker ran on `max`, or `limit_restarts` is now above 3: every account is spent (`max` is
  yours too). **Stop**: start nothing, `CX wait` for the workers still in flight, record every
  ticket in the state file, and write the hand-off file ([handoff.md](handoff.md), step 3) with
  **Next action** "Resume after <the reset time in `~/.claude/accounts/runs/<name>.limit.txt`>".
  Write the successor's prompt file as handoff.md step 4 does but do not run it (it would hit
  the same limit): comment the reset time and that exact `cx run --detach yes` command on the
  feature issue, send a push notification when that tool exists, and end your turn.
- Otherwise run `CX pick` again and start the same kind of worker on that account in the same
  place: an implementer as `ticket-<n>-r<restarts + 1>` (add one to `restarts`), mode `herdr`,
  plus the line "Continue from the commits already on this branch: read
  `git log feature/<slug>..HEAD` first."; a flow checker under its own name again.

## Finish — every ticket closed or parked

1. **Parked tickets.** If any ticket is parked, do step 3, then steps 5 and 6, and skip the merge.
2. **User-flow check**, once per feature. Skip it only when the spec adds or changes nothing a
   user does (no page, command, endpoint or file a user touches), and say so in the PR body.
   Otherwise fill [flow-check.md](flow-check.md) and run it in the main checkout:
   `CX run --name flow-<slug>-<flow_checks> --account "$(CX pick)" --cwd "$ROOT" --model opus
   --effort medium --prompt-file <file>`, then wait as in loop step 3 and add one to
   `flow_checks`. Its last line decides:
   - `FLOWS PASS` → step 3.
   - `FLOWS PARTIAL` (it ran out of context) → run it again with `<ONLY>` set to the flows it
     listed as `SKIPPED`; this does not count as a check.
   - `FLOWS FAIL` → one fix ticket per failed flow: a sub-issue of the feature, labels `ticket`
     and `ready-for-agent`, body with a plain-language summary, `## Breaks` (the spec line the
     flow belongs to) and `## Reproduction` (the checker's steps, expected against actual). Run
     them through the **Loop**, then run the check once more with `<ONLY>` set to the flows that
     failed. A second `FLOWS FAIL` goes to step 3 with the merge skipped; the PR body lists the
     flows that still fail.
3. **Docs.** Set `Status: implemented` in `docs/specs/<slug>.md`. Add an entry at the top of
   `CHANGELOG.md` (create it with a `# Changelog` heading when missing): `## <Feature name> —
   <yyyy-mm-dd>` and two to four plain lines on what changed for the user. Commit.
4. **Catch up with `main`.** `git fetch origin`. When `origin/main` has commits the branch lacks
   (`git merge-base --is-ancestor origin/main HEAD` fails), `git merge --no-edit origin/main` and
   run the typecheck and the full suite again; red skips the merge, with the output in the PR
   body.
5. **Push.** `git push -u origin feature/<slug>`.
6. **PR.** `gh pr create --base main --head feature/<slug> --title "<Feature name>" --body-file
   <file>` with: a plain-language summary, the closed tickets, the parked tickets and why,
   `Closes #<feature issue>`, how it was tested (suite, review, the flows walked and their
   result), and no "Generated with Claude" line. Comment the PR link on the feature issue.
7. **Merge** — only when nothing is parked, nothing above was red and the flow check passed or was
   skipped. When the PR has checks (`gh pr checks <pr>` lists any), wait for them with
   `gh pr checks <pr> --watch --fail-fast` under a Bash timeout of `600000`; a failing check
   leaves the PR open. Then `gh pr merge <pr> --merge` and run Phase C of workflow.md.
8. **Report** as a comment on the feature issue, since nobody reads a headless session: what was
   built and merged, or why the PR was left open; every ticket labelled `needs-info` or
   `ready-for-human`; the flows that were walked. Send it as a push notification too when that
   tool exists, and repeat it as your last message.
