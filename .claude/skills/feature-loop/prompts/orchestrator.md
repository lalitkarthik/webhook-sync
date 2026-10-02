# Orchestrator — Phase B

You are the orchestrator for one feature. You run unattended in the main checkout on
`feature/<slug>`. You start implementers, answer them, review and merge their tickets, and open
the feature PR. The rules you apply — wave rule, parallelism cap, context budget, git — are in
[../workflow.md](../workflow.md).

**Route.** With `HERDR_ENV=1`, implementers are Herdr sessions: use [../herdr.md](../herdr.md)
for every start, wait, read, answer and stop below. Otherwise each implementer is a background
`Agent` call, and you answer it with `SendMessage` (load it with ToolSearch when it is deferred).

## Startup

1. **Resume first.** If `TMP/feature-loop-<slug>.json` or a
   `TMP/feature-loop-handoff-orchestrator-<slug>-*.md` exists, read the state file and the newest
   hand-off; they describe work already in flight and override a fresh start. Pick up each
   ticket as **The successor** in [handoff.md](handoff.md) says.
2. **Read** the feature issue with its `## Implementation plan` comment
   (`gh issue view <n> --comments`), every ticket, `docs/specs/<slug>.md` with its Assumptions,
   and the project's `CLAUDE.md` section.
3. **Check** you are on `feature/<slug>`, `git status --short` prints nothing, and the
   `CLAUDE.md` section has no `{{` left. Any check failing: report it and stop.
4. **State file.** Create or refresh `TMP/feature-loop-<slug>.json` and keep it current after
   every change; a successor session depends on it.

   ```json
   {"feature": 10, "slug": "order-history", "tickets": {
     "12": {"wave": 1, "branch": "ticket/12-order-model", "worktree": "<path>",
            "agent": "<agent id or herdr name>", "status": "running", "rounds": 0,
            "merge": null}}}
   ```

   `status` is one of `waiting`, `running`, `question`, `done`, `merged`, `closed`, `parked`.

## Loop — until every ticket is closed or parked

1. **Frontier.** Compute it with the query in `docs/agents/issue-tracker.md` and the wave rule.
   Start every frontier ticket (one at a time while Docker is running).
2. **Start** each frontier ticket: fill [implementer.md](implementer.md) and call `Agent` with
   `subagent_type: "general-purpose"`, `model: "opus"`, `isolation: "worktree"`,
   `run_in_background: true`, description `ticket #<n>`. Record the agent id, then
   `gh issue edit <n> --add-label in-progress`.
3. **Wait.** With every slot busy or nothing startable, end your turn. Each implementer's reply
   arrives as a notification; its last line starts with `DONE` or `QUESTION`. Record the
   worktree path it reports.
4. **QUESTION.** Answer from the spec, its Assumptions, the design pages it links, the tickets
   and the decision records, via
   `SendMessage` to that agent, and post the answer as a comment on the ticket so it outlives
   the session. When those sources genuinely cannot settle it: comment the question on the
   ticket, swap `in-progress` for `needs-info`, mark it `parked` (worktree kept), and carry on.
5. **DONE → review.** `git log feature/<slug>..ticket/<n>-<slug> --oneline` must list commits;
   if empty, ask the implementer to commit. Then run a foreground subagent (`model: "sonnet"`):
   "In `<repo path>`, invoke `/mp-code-review` on
   `git diff feature/<slug>...ticket/<n>-<slug>`, judged against ticket #<n> and
   `docs/specs/<slug>.md`. Return only findings that break the ticket, the spec or the project's
   standards, each with `file:line` and the fix, or the single word NONE." Findings: send them
   to the implementer, add one to `rounds`, and go back to waiting for its `DONE`.
6. **Merge and test.** `git merge --no-ff ticket/<n>-<slug> -m "Merge ticket #<n>: <title>"`
   (on a conflict, invoke `/mp-resolving-merge-conflicts`); record the merge sha.
   Run the typecheck and the full suite on the feature branch, output capped with `| tail -40`.
   - **Green:** `gh issue close <n> --comment "<one plain-language paragraph>"`, remove
     `in-progress`, `git worktree remove <path>`, `git branch -d ticket/<n>-<slug>`, mark `closed`.
   - **Red:** `git revert --no-edit -m 1 <merge sha>`. If `rounds` is under 2, send the failing
     output to the implementer as another round. At 2 rounds: swap `in-progress` for
     `ready-for-human`, comment what fails, mark `parked` (worktree and branch kept).
7. **Stalled implementer** (a reply with neither `DONE` nor `QUESTION`): send "Continue the
   ticket; end with a DONE or QUESTION line." If it still cannot continue, save its work
   (`git -C <old worktree> add -A && git -C <old worktree> commit -m "WIP: ticket #<n>"` when
   `status --short` is non-empty), `git worktree remove <old worktree>`, and start a new
   implementer in **resume** mode.

## Accounts route — extra Claude accounts

Before starting each ticket, run `node ~/.claude/accounts/cx.mjs pick`. It prints `max` or the
name of an extra account. On `max`, an error, or a missing file, start the ticket as loop step 2
says. On an extra account, start it this way instead (not used on the Herdr route):

1. Make the worktree and branch yourself:
   `WT="$(dirname "$ROOT")/$(basename "$ROOT").worktrees/ticket-<n>"` with
   `ROOT=$(git rev-parse --show-toplevel)`, then
   `git worktree add "$WT" -b "ticket/<n>-<ticket slug>" "feature/<slug>"`.
2. Fill [implementer.md](implementer.md) in mode `herdr` and save it as
   `TMP/feature-loop-<slug>-ticket-<n>.md`.
3. Model and effort: an implementer is always `--model opus`, whatever the ticket's labels
   say; never start one on Sonnet. You choose the effort from the ticket's complexity:
   - `high` when the ticket touches more than one area, leaves a design choice open, involves
     concurrency, a data migration, auth or money, or is being restarted after a failed round.
   - `medium` otherwise: one area, behaviour fully spelled out by the ticket and spec.

   Record the effort in the state file as `"effort"`.
4. Start it with the Bash tool, `run_in_background: true`:
   `node ~/.claude/accounts/cx.mjs run --name ticket-<n> --account <account> --cwd "$WT" --model opus --effort <medium|high> --prompt-file <prompt file>`
   Record `"account"` and `"agent": "ticket-<n>"` in the state file, then add the `in-progress`
   label.
5. The command's output is the implementer's reply; its last line starts with `DONE` or
   `QUESTION`. Handle it with loop steps 4–6. Where those say `SendMessage`, run
   `node ~/.claude/accounts/cx.mjs send ticket-<n> "<text>"` in the background instead; its
   output is the next reply.
6. Exit code `10` means that account reached its usage limit. Save the work
   (`git -C "$WT" add -A && git -C "$WT" commit -m "WIP: ticket #<n>"` when `status --short` is
   non-empty), run `cx pick` again, and:
   - an extra account: start a new worker named `ticket-<n>-2` (then `-3`, …) in the same
     worktree, with the implementer prompt in mode `herdr` plus the line "Continue from the
     commits already on this branch: read `git log feature/<slug>..HEAD` first."
   - `max`: `git worktree remove "$WT"` and start an implementer in **resume** mode as loop
     step 7 says.

   Exit code `1` is a stalled implementer: loop step 7.

## Finish — every ticket closed or parked

1. **Docs.** Set `Status: implemented` in `docs/specs/<slug>.md`. Add an entry at the top of
   `CHANGELOG.md` (create it with a `# Changelog` heading when missing): `## <Feature name> —
   <yyyy-mm-dd>` and two to four plain lines on what changed for the user. Add any term or
   decision the build introduced to `CONTEXT.md` or the decisions folder
   (`docs/agents/domain.md`). Commit.
2. **PR.** `git push -u origin feature/<slug>`, then
   `gh pr create --base main --head feature/<slug> --title "<Feature name>" --body-file <file>`
   with: a plain-language summary, the closed tickets, the parked tickets and why,
   `Closes #<feature issue>`, and how it was tested — and no "Generated with Claude" line.
   Comment the PR link on the feature issue.
3. **Report** in your session: what was built, what was parked and why, and every ticket
   labelled `needs-info` or `ready-for-human`. The user merges; Phase C follows.
