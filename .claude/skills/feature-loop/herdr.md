# Herdr recipes (only when `HERDR_ENV=1`)

In the Herdr route every role is a full Claude Code session in its own Herdr tab: the planning
session launches the orchestrator, the orchestrator launches implementers and verifiers, and
any session but a worker launches its own successor at 40%; a worker hands off to its
orchestrator ([prompts/handoff.md](prompts/handoff.md), **Workers**). Everything else in the
workflow is unchanged. For Herdr itself, see the `herdr` skill.

Read every id from the JSON Herdr returns. `jget` pulls one field out of it (no `jq` needed):

```bash
jget() { python -c "import json,sys,functools; print(functools.reduce(lambda d,k: d[k], sys.argv[1:], json.load(sys.stdin)))" "$@"; }
```

Variables: `SLUG` feature slug, `N` ticket number, `FEATURE` feature issue number, `TSLUG`
ticket slug, `AGENT`, `MODEL` and `EFFORT` the implementer's definition, model and effort, read
from the ticket's `worker:` label ([prompts/orchestrator.md](prompts/orchestrator.md), loop step
2.1: `fl-impl-sonnet-high` / `sonnet` / `high`, `fl-impl-opus-medium` / `opus` / `medium`,
`fl-impl-opus-high` / `opus` / `high`), `HANDOFF` hand-off path, `PROMPT` a file holding a
filled prompt.

## Launch the orchestrator (Phase A, Hand off)

```bash
test "$HERDR_ENV" = 1 || { echo "not inside Herdr"; exit 1; }
OUT=$(herdr tab create --workspace "$HERDR_WORKSPACE_ID" --cwd "$PWD" --label "orchestrator #$FEATURE" --no-focus)
PANE=$(printf '%s' "$OUT" | jget result root_pane pane_id)
herdr agent start "orch-$SLUG" --kind claude --pane "$PANE" --timeout 90000 -- --model opus --effort medium --dangerously-skip-permissions --name "orchestrator #$FEATURE"
herdr agent prompt "orch-$SLUG" "/feature-loop orchestrate #$FEATURE"
```

## Launch an implementer (orchestrator, loop step 2)

The orchestrator makes the worktree and branch itself, so the filled
[prompts/implementer.md](prompts/implementer.md) uses mode `herdr`: skip its `new`/`resume` git
lines and run only the setup command.

```bash
ROOT=$(git rev-parse --show-toplevel)
WT="$(dirname "$ROOT")/$(basename "$ROOT").worktrees/ticket-$N"
git worktree add "$WT" -b "ticket/$N-$TSLUG" "feature/$SLUG"
OUT=$(herdr tab create --workspace "$HERDR_WORKSPACE_ID" --cwd "$WT" --label "ticket #$N" --no-focus)
PANE=$(printf '%s' "$OUT" | jget result root_pane pane_id)
TAB=$(printf '%s' "$OUT" | jget result tab tab_id)
herdr agent start "ticket-$N" --kind claude --pane "$PANE" --timeout 90000 -- --agent "$AGENT" --model "$MODEL" --effort "$EFFORT" --dangerously-skip-permissions --name "ticket #$N"
herdr agent prompt "ticket-$N" "$(cat "$PROMPT")"
```

`--agent` is what gives the session its Stop and protect-tests hooks: never start an implementer
without it. Record the worktree, tab id and agent name in the state file. A resumed ticket keeps
its worktree and branch: skip `git worktree add`, and tell the implementer to continue from the
commits already on the branch.

## Launch a verifier (orchestrator, loop step 5.4 and Finish)

`PROMPT` holds the filled [prompts/verifier.md](prompts/verifier.md); the working directory is
the ticket's worktree (`$WT`), or the main checkout for the feature modes. `--agent fl-verifier`
gives it the read-only sandbox and the verdict hook.

```bash
OUT=$(herdr tab create --workspace "$HERDR_WORKSPACE_ID" --cwd "$WT" --label "verify #$N" --no-focus)
PANE=$(printf '%s' "$OUT" | jget result root_pane pane_id)
herdr agent start "verify-$N" --kind claude --pane "$PANE" --timeout 90000 -- --agent fl-verifier --model opus --effort medium --dangerously-skip-permissions --name "verify #$N"
herdr agent prompt "verify-$N" "$(cat "$PROMPT")"
```

Wait for it and read its reply as below, then run the `verdict.py` check of loop step 5.4 and
close its tab.

## Watch, read, answer, stop

Set the Bash tool timeout to 600000 for waits. Between waits, check every session's context use:
`python scripts/claude/context-guard.py --report`. An implementer at 40% commits its work and
replies `HANDOFF`, so none should show the over-45% flag. Close its tab and start a fresh one
with the implementer recipe (skip `git worktree add`) as `ticket-$N-r<k>`
(orchestrator.md, loop step 7); record the current name in the state file.

```bash
herdr agent list
herdr agent wait "ticket-$N" --timeout 540000
herdr agent read "ticket-$N" --source recent-unwrapped --lines 120
herdr agent prompt "ticket-$N" "<answer, review findings, or failing output>"
herdr agent send-keys "ticket-$N" esc
herdr tab close "$TAB"
```

Close an implementer's tab once its ticket is closed or parked, before removing its worktree.

## Successor on a context hand-off (orchestrator, audit orchestrator, planning, design)

```bash
OUT=$(herdr tab create --workspace "$HERDR_WORKSPACE_ID" --cwd "$PWD" --label "<same label> (2)" --no-focus)
PANE=$(printf '%s' "$OUT" | jget result root_pane pane_id)
herdr agent start "<name>-2" --kind claude --pane "$PANE" --timeout 90000 -- --model <same model> --effort <same effort> --dangerously-skip-permissions --name "<same display name> (2)"
herdr agent prompt "<name>-2" "Read $HANDOFF. You are the successor of <name>. First run: herdr tab close $HERDR_TAB_ID. Then continue from its Next action."
herdr agent get "<name>-2"
```

`herdr agent get` must show `working` or `idle` before you end your turn. At the end of the
build, announce the PR: `herdr notification show "Feature $SLUG ready for review" --body "PR #<pr>" --sound done`.
