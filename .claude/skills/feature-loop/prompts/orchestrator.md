# Orchestrator — Phase B

You are the orchestrator for one feature. You run unattended in the main checkout on
`feature/<slug>`. You start a worker per ticket, put each finished ticket through the gate, the
review and the verifier, merge it into the feature branch, verify the whole feature, and merge it
into `main`. The rules you apply (wave rule, models, context budget, git, fix line) are in
[../workflow.md](../workflow.md); commands are in the project's `CLAUDE.md` section and
`scripts/claude/fl-config.json`. `CX` means `node ~/.claude/accounts/cx.mjs`.

**Never end your turn while work is in flight.** You may be running headless (`claude -p`), where
ending the turn ends the session and every worker it started. To wait, run `CX wait --since <ms>
<names…>` (it blocks up to 9 minutes; run it again until it prints a worker). When nothing is in
flight and nothing can start, you are done (**Finish**) or parked; only then end the turn. The
two other ways to end it: after a hand-off has started your successor ([handoff.md](handoff.md)),
and the **Usage limit** stop.

**Route.** Every worker (implementer, fixer, gardener, verifier) is a `CX run` process whose
working directory is its worktree, started with the Bash tool and `run_in_background: true`; so is
every `CX send` (a fix round runs as long as a first start, and a foreground call is killed by the
Bash tool's timeout, taking the worker with it). Each start or send sets the worker's `started` in
the state file to the time in ms. A send or run that exits `10` goes to the **Usage limit** rule
at once; never wait on that worker. `CX pick` names the account (`max` or an extra one). With
`HERDR_ENV=1`, start and answer them with the recipes in [../herdr.md](../herdr.md) instead,
passing the same `--agent`. The test author and the reviewer are `Agent` subagents in the main
checkout.

## Startup

1. **Resume first.** If `TMP/feature-loop-<slug>.json` or a
   `TMP/feature-loop-handoff-orchestrator-<slug>-*.md` exists, read the state file and the newest
   hand-off; they describe work already in flight and override a fresh start. Pick up each ticket
   as **The successor** in [handoff.md](handoff.md) says.
2. **Read** the feature issue with its `## Implementation plan` comment
   (`gh issue view <n> --comments`), every ticket, `docs/specs/<slug>.md` with its Assumptions,
   and the project's `CLAUDE.md` section.
3. **Check** you are on `feature/<slug>`, `git status --short` prints nothing, the `CLAUDE.md`
   section has no `{{` left, `scripts/claude/ticket_gate.py` and `.claude/skills/verify-*/SKILL.md`
   exist, and `python scripts/claude/ac_check.py --issue <t> --expect red` passes for every open
   ticket not labelled `gardener` (a gardener ticket has no tagged test yet). When resuming (the
   state file exists), two checks relax: `git status --short` may list
   `docs/agents/findings.tsv` (it stays uncommitted until Finish) and nothing else, and `ac_check`
   runs only for tickets whose status is `waiting` and that are not labelled `gardener`. Any
   check failing: report it and stop (the repo needs `/feature-loop setup`, or Phase A did not
   finish).
4. **State file.** Create or refresh `TMP/feature-loop-<slug>.json` and keep it current after
   every change; a successor depends on it.

   ```json
   {"feature": 10, "slug": "order-history", "closed_acs": ["AC-12-1"], "tickets": {
     "12": {"wave": 1, "branch": "ticket/12-order-model", "worktree": "<path>",
            "agent": "ticket-12", "started": 1759480000000, "worker": "sonnet-high", "failures": 0,
            "last_failure": "", "restarts": 0, "limit_restarts": 0, "status": "running",
            "merge": null, "acs": ["AC-12-1", "AC-12-2"]}}}
   ```

   `status` is one of `waiting`, `running`, `question`, `gate`, `review`, `verify`, `done`,
   `merged`, `closed`, `parked`.

## Loop — until every ticket is closed or parked

1. **Frontier.** Compute it with the query in `docs/agents/issue-tracker.md` and the wave rule.
   Leave out every parked ticket (labelled `ready-for-human` or `needs-info`, or `parked` in the
   state file), even when an older copy of the query lists it. Start every frontier ticket (one at
   a time while Docker is running).
2. **Start** each frontier ticket:
   1. Worker from its label: `worker:sonnet-high` → `fl-impl-sonnet-high` (`--model sonnet
      --effort high`), `worker:opus-medium` → `fl-impl-opus-medium` (`--model opus --effort
      medium`), `worker:opus-high` → `fl-impl-opus-high` (`--model opus --effort high`). No label:
      `opus-medium`. Record it as `"worker"`.
   2. Worktree: `ROOT=$(git rev-parse --show-toplevel)`,
      `WT="$(dirname "$ROOT")/$(basename "$ROOT").worktrees/ticket-<n>"`,
      `git worktree add "$WT" -b "ticket/<n>-<ticket slug>" "feature/<slug>"`.
   3. Ticket file: write `TMP/feature-loop-<slug>/ticket-<n>.json` as
      `{"issue": <n>, "base": "feature/<slug>", "areas": [<areas from the plan>], "acs": [<AC IDs
      from the issue>], "allow": [], "evidence_dir": ""}` and copy it to `$WT/.fl-ticket.json`.
      `allow` lists a lockfile only when the ticket says it adds a dependency.
   4. Fill [implementer.md](implementer.md) in mode `herdr` and save it as
      `TMP/feature-loop-<slug>-ticket-<n>.md`.
   5. `CX run --name ticket-<n> --account "$(CX pick)" --cwd "$WT" --agent <definition> --model
      <m> --effort <e> --prompt-file <file>` in the background. Record `"agent": "ticket-<n>"`,
      the account, and `started`; `gh issue edit <n> --add-label in-progress`.
3. **Wait.** `CX wait --since <earliest started of the workers in flight> <their names>`. Each
   line it prints is one worker's reply, `{"name", "exit", "result", "at"}`. A reply whose `at` is
   before that worker's `started` belongs to an earlier run: ignore it and wait again. Otherwise
   handle the reply (`result`) by its last line: `DONE` → step 5, `QUESTION` → step 4, `HANDOFF`
   → step 7 (restart at once); exit `10` → **Usage limit**; anything else → step 7.
4. **QUESTION.** Answer from the spec, its Assumptions, the design pages it links, the tickets and
   the decision records with `CX send <name> "<answer>"` in the background, and post the answer
   as a comment on the ticket. When those sources genuinely cannot settle it: comment the
   question on the ticket, swap `in-progress` for `needs-info`, mark it `parked` (worktree kept).
5. **DONE → gate, review, verify.** A failure at any of the three is one **failure** (step 8).
   Set `status` to `done` on the reply, then `gate`, `review` and `verify` as each begins.
   1. `git log feature/<slug>..ticket/<n>-<slug> --oneline` must list commits, and
      `git -C "$WT" status --porcelain` must print nothing; otherwise ask the worker to commit
      everything (`CX send`, in the background) and wait again. This is not a failure.
   2. **Gate.** `git -C "$WT" merge --no-edit feature/<slug>` (a conflict: invoke
      `/mp-resolving-merge-conflicts` in `$WT`), then
      `LOG=TMP/feature-loop-<slug>/gate-<n>.log`,
      `python scripts/claude/ticket_gate.py --base feature/<slug> --root "$WT" --ticket
      TMP/feature-loop-<slug>/ticket-<n>.json --config scripts/claude/fl-config.json --acs
      <closed_acs, comma-separated> > "$LOG" 2>&1; rc=$?; tail -80 "$LOG"`. The ticket and the
      config are your copies in the main checkout, never the worktree's. Branch on `rc`, never on
      what the tail shows (the tail can cut off a `FAIL <name>:` header). `rc` 1: the failure
      report is every `FAIL` line of the log (with the output under it) plus its last 80 lines.
   3. **Review.** A foreground `fl-opus-medium` subagent: "In `<WT>`, run `git rev-parse HEAD` and
      put it on your first line as `SHA <sha>`. Invoke `/mp-code-review` on
      `git diff feature/<slug>...HEAD`, judged against ticket #<n>, `docs/specs/<slug>.md` and the
      project's written standards (`CLAUDE.md`, `CONTEXT.md`, decision records). Report only
      findings that break the ticket, the spec or a written standard (P0 or P1), one per line as
      `<category in 1-3 words> | <file:line> | <what breaks> | <the fix>`, or the single word
      NONE." Findings: append each to `docs/agents/findings.tsv` (in the main checkout,
      uncommitted until Finish); the failure report is the findings.
   4. **Verify.** `SHA=$(git -C "$WT" rev-parse HEAD)`,
      `EVID="$ROOT/verification/<n>/${SHA:0:7}"`; set `"evidence_dir": "$EVID"` in both ticket
      files. Fill [verifier.md](verifier.md) in mode `ticket` (`<AC_LIST>` = this ticket's ACs,
      `<AC_CMD>` = `ac_cmd` filled with them, `<FEATURE_FILES>` = the feature files the ticket
      touches) and run `CX run --name verify-<n>-<failures> --account "$(CX pick)" --cwd "$WT"
      --agent fl-verifier --model opus --effort medium --prompt-file <file>`; wait for it as in
      step 3. Then check the verdict yourself, since the verifier's own hook gives up after three
      blocks: `python scripts/claude/verdict.py --dir "$EVID" --sha "$SHA" --acs <this ticket's
      ACs, comma-separated>`, and branch on its exit code and first word, never on what
      `verdict.json` claims:
      - `0` `PASS` → step 5.5.
      - `1` `FAIL`: append one row per entry it lists to `findings.tsv`, its category the kind of
        mistake in 1–3 words as the reviewer writes it (what broke, never `verifier`); the failure
        report is the lines it printed (id, status, `repro`, evidence paths) — never the
        verifier's transcript.
      - `1` `INVALID`: the verifier failed, not the ticket. Run it once more as
        `verify-<n>-<failures>-2`; a second `INVALID` parks the ticket (step 8, **Park**) with the
        printed problems as the report.
   5. All three passed → step 6.
6. **Merge and test.** One serialized step: no other ticket is merged into `feature/<slug>`
   between this merge and its CI run, so on red the failed merge is always the tip.
   `git merge --no-ff ticket/<n>-<slug> -m "Merge ticket #<n>: <title>"` and record the merge
   sha in the ticket's `merge` entry of the state file. On the feature branch,
   `LOG=TMP/feature-loop-<slug>/ci-<n>.log`, `python scripts/claude/ci_check.py --acs
   <closed_acs + the ACs in this ticket's ticket file> > "$LOG" 2>&1;
   rc=$?; tail -40 "$LOG"`. Branch on `rc` (0 green, 1 red), never on the tail.
   - **Green:** add the ACs in the ticket's ticket file to `closed_acs`;
     `gh issue close <n> --comment "<one plain-language paragraph>"`; remove `in-progress`;
     `git worktree remove "$WT"`; `git branch -d ticket/<n>-<slug>`; mark `closed`.
   - **Red:** `git reset --keep <merge sha>^1`, with the sha from the state file's `merge`
     entry (the merge is the tip, so this undoes it and keeps the uncommitted `findings.tsv`);
     then set `merge` back to `null`. Never `git revert` the merge: the revert would flow back
     into the ticket branch at the next gate and delete its work, and a later re-merge would not
     bring the reverted commits back. `feature/<slug>` is not pushed before Finish step 6, so the
     reset rewrites nothing shared. The failure report is every `FAIL` line of
     `TMP/feature-loop-<slug>/ci-<n>.log` (with the output under it) plus its last 40 lines
     (step 8).
7. **Stalled or handed-off worker.** Neither `DONE` nor `QUESTION`: `CX send <name> "Continue the
   ticket; end with a DONE or QUESTION line."` in the background. If it still cannot continue, or
   its last line is `HANDOFF` (it reached its context budget and committed its work; never
   resume that session), restart it: save any uncommitted work
   (`git -C "$WT" add -A && git -C "$WT" commit -m "WIP: ticket #<n>"` when `status --short` is
   non-empty), add one to `restarts`, and start a new worker in the same worktree with the same
   agent as `ticket-<n>-r<restarts>`, mode `herdr` (the worktree is already on the ticket
   branch), with `<FAILURE_REPORT>` holding the heading "## Where the last worker stopped", its
   `HANDOFF` line when there is one, and "Continue from the commits already on this branch: read
   `git log feature/<slug>..HEAD` first." Record it as the ticket's `agent`.
8. **Failure and escalation.** Add one to `failures`. If the first line of this failure report
   equals `last_failure`, the worker is not making progress: park now. Otherwise store it and
   pick the next worker:

   | Label | failure 1 | failure 2 | failure 3 | failure 4 |
   |---|---|---|---|---|
   | `sonnet-high` | opus-medium | opus-medium | opus-high | park |
   | `opus-medium` | opus-medium | opus-medium | opus-high | park |
   | `opus-high` | opus-high | park | | |

   The same worker as before: `CX send <name> "<failure report>\nFix this, commit, and end with
   DONE or QUESTION."` in the background. A different worker: commit any uncommitted work as
   `WIP: ticket #<n>`, fill [implementer.md](implementer.md) in mode `herdr` with the failure
   report in `<FAILURE_REPORT>` under the heading "## Fix this first", and start it in the same
   worktree as `ticket-<n>-<worker>`. **Park:** comment the last failure report on the ticket, swap
   `in-progress` for `ready-for-human`, mark `parked` (worktree and branch kept).
9. **Gardener.** After each merge, count rows per category in `docs/agents/findings.tsv`. A
   category is the kind of mistake (`unvalidated input`, `missing await`); never count rows whose
   category is a pipeline stage such as `verifier`, `review` or `minor` (an older log may hold
   them). A category reaching 2 rows with no open or closed `gardener` ticket for it: create a
   sub-issue of the feature titled `Make <category> impossible to repeat`, labels `ticket`,
   `ready-for-agent`, `gardener`, `worker:opus-medium`, body with the rows and
   `## Acceptance criteria` /
   `- AC-<new>-1 [unit] the new rule fails on the pattern and passes on the fixed code`. It joins
   the frontier with areas `[]`; start it with [gardener.md](gardener.md) added to its implementer
   prompt. The gardener writes its own acceptance test, as a new file in `tests/acceptance/`
   tagged `AC-<new>-1`, so skip the `ac_check` step for it (there is no test until the gardener
   writes it). Everything else is as for any ticket: `"acs": ["AC-<new>-1"]` in its ticket file,
   so the Stop hook, the gate and the verifier all run that test by its ID.

**Usage limit** (a worker exits `10`: implementer, fixer, gardener or verifier). Save the work as
a `WIP: ticket #<n>` commit when `status --short` is non-empty, and add one to the ticket's
`limit_restarts`.
- The worker ran on `max`, or `limit_restarts` is now above 3: every account is spent (`max` is
  yours too). **Stop**: start nothing, `CX wait` for the workers still in flight, record every
  ticket in the state file, and write the hand-off file ([handoff.md](handoff.md), step 3) with
  **Next action** "Resume after <the reset time in `~/.claude/accounts/runs/<name>.limit.txt`>".
  Write the successor's prompt file as handoff.md step 4 does but do not run it (it would hit
  the same limit): comment the reset time and that exact `cx run --detach yes` command on the
  feature issue, send a push notification when that tool exists, and end your turn.
- Otherwise run `CX pick` again and start the same agent on that account in the same worktree:
  an implementer as `ticket-<n>-r<restarts + 1>` (add one to `restarts`), mode `herdr`, plus the
  line "Continue from the commits already on this branch: read `git log feature/<slug>..HEAD`
  first."; a verifier under its own name again (its run file is cleared when it starts).

## Finish — every ticket closed or parked

1. **Parked tickets.** If any ticket is parked, commit `docs/agents/findings.tsv` and skip to
   step 6: push, then open the PR without merging.
2. **Explore.** Write `$ROOT/.fl-ticket.json` as `{"issue": <feature>, "base": "main",
   "evidence_dir": "$ROOT/verification/feature-<slug>/explore-<sha7>"}` and run the verifier in
   mode `feature-explore` in the main checkout (`--name verify-feature-<slug>-explore`,
   `<AC_LIST>` = every ticket's ACs, `<FEATURE_FILES>` = the feature files this feature touches).
   Sort each non-`PASS` entry by the fix line in workflow.md:
   - **above the line** → a fix ticket: sub-issue of the feature, labels `ticket`,
     `ready-for-agent`, `worker:opus-medium`, sections `## Reproduction` (the `repro`) and
     `## Acceptance criteria` with `- AC-<new>-1 [<check type>] <the promised behaviour>`; then
     an `fl-test-author` subagent in mode `acceptance` for those tickets, and back to the
     **Loop**;
   - **minor** → one row in `findings.tsv`, its category the kind of mistake in 1–3 words (what
     broke, never `minor`) and its summary starting `minor:`.
   At most 2 explore passes; above-the-line findings left after the second become parked fix
   tickets.
3. **Journey tests.** An `fl-test-author` subagent in mode `durable` with the last explore
   verdict. It commits the journey and smoke tests.
4. **Docs and metrics.** Set `Status: implemented` in `docs/specs/<slug>.md`. Add an entry at the
   top of `CHANGELOG.md` (create it with a `# Changelog` heading when missing):
   `## <Feature name> — <yyyy-mm-dd>` and two to four plain lines on what changed for the user.
   Add new terms and decisions to `CONTEXT.md` or the decisions folder. Append one row to
   `docs/agents/metrics.md` from the state file. Commit these with `docs/agents/findings.tsv`.
5. **Confirm.** `SHA=$(git rev-parse HEAD)`, evidence
   `$ROOT/verification/feature-<slug>/${SHA:0:7}`: rewrite `$ROOT/.fl-ticket.json` with that
   `evidence_dir` (still holding the explore folder from step 2, it would make the verifier's
   hook check the explore verdict), then run the verifier in mode `feature-confirm`
   (`--name verify-feature-<slug>-confirm`). A `FAIL` sends its entries through step 2's sorting
   once; a second confirm `FAIL` → step 6 without merging.
6. **Push.** `git push -u origin feature/<slug>`.
7. **PR.** `gh pr create --base main --head feature/<slug> --title "<Feature name>" --body-file
   <file>` with: a plain-language summary, the closed tickets, the parked tickets and why,
   `Closes #<feature issue>`, how it was tested (gate, review, verifier verdict path), and no
   "Generated with Claude" line. Comment the PR link on the feature issue.
8. **Merge** (only when nothing is parked and confirm passed): `git fetch origin`, then
   `python scripts/claude/automerge.py --pr <pr> --verdict <confirm verdict path> --wait 300`,
   run with a Bash tool timeout above the wait (10 minutes, `600000` ms). The script detects a
   ruleset on `main` itself and arms auto-merge when there is one; without one it waits up to
   `--wait` seconds for the PR's checks before merging. Do not call `gh api` for rulesets (the
   repo's deny list blocks it) and do not pass `--auto`.
   - `0` `MERGED`: run Phase C of workflow.md now. `ARMED`: `gh pr view <pr> --json state` until
     `MERGED` (re-check with `CX wait`-sized pauses: `python -c "import time; time.sleep(300)"`),
     then Phase C.
   - `3` `HOLD`: report the held issue; the user decides.
   - `4`: auto-merge is switched off; report it.
   - `1` with output starting `PENDING` (the checks were still running after the wait): pause as
     for `ARMED` and run the same `automerge.py` command again, at most 6 times; still `PENDING`
     after that: report it as an error.
   - `1` otherwise (`ERROR …`): report the error.
9. **Report** in your session: what was built and merged, what was parked or held and why, and
   every ticket labelled `needs-info` or `ready-for-human`.
