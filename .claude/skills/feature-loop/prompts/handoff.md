# Hand off

Used at the end of Phase A (role `orchestrator`) and whenever the context guard says "hand off
now" (your own role). The file format is the project's `docs/agents/handoff.md`; the file is
`TMP/feature-loop-handoff-<role>-<slug>-<yyyymmdd-hhmm>.md`. `CX` means
`node ~/.claude/accounts/cx.mjs`.

## On the 40% notice

The guard says "hand off now" at 40% so the hand-off is finished before the hard limit of 45%.
Once it reports the session over 45% (it then repeats on every call), skip step 2 — record each
worker still running as `running` — and do nothing but steps 3 to 5: no other file is read
and no other command is run.

A worker (implementer, fixer, flow checker) does not follow these steps: see **Workers**.

1. Finish the tool call in progress. From here on, start no new ticket, review or research.
2. **Orchestrator and audit orchestrator:** run `CX wait` (orchestrator.md, loop step 3) until
   every worker in flight has replied, a `CX send` still running included, and handle none of
   the replies. Never end your turn to wait: you may be headless, and ending the turn ends the
   session and every worker it started. Record each ticket in the state file as `done` or
   `question` (with the question), with its worktree path.
3. Write the hand-off file. Its **Next action** is one precise instruction.
4. Start exactly one successor:
   - **Orchestrator and audit orchestrator** (unattended, maybe headless: never ask the user to
     open a session, and never end the turn without a successor): write
     `TMP/feature-loop-<orch|audit>-<slug>-<k>.md` holding the launch prompt plus "Read the
     hand-off <path> first." The orchestrator's launch prompt is the one in workflow.md Phase A
     step 10; the audit orchestrator's is "You are the feature-loop audit orchestrator. Read
     `.claude/skills/feature-loop/workflow.md` and
     `.claude/skills/feature-loop/prompts/audit.md`, then start at Startup<, for feature
     #<n>>." Run `CX run --detach yes --name <orch-<slug>|audit-<yyyymmdd>>-<k> --account max
     --cwd <checkout> --model opus --effort medium --prompt-file <that file>` and check it
     prints `started <name>`. With `HERDR_ENV=1`, start it with the recipe in
     [../herdr.md](../herdr.md) instead (never both) and confirm it is running. Also send a push
     notification when that tool exists.
   - **Planning and design**, `HERDR_ENV=1`: start the successor with the recipe in
     [../herdr.md](../herdr.md) and confirm it is running.
   - **Planning and design**, default (you cannot open a session for the user): print the exact
     command that starts the successor, `cd <checkout> && claude "/feature-loop plan <hand-off
     path>"` (role `design`: `/feature-loop design <hand-off path>`). When this session has a
     tool that spawns a new task or session (the desktop app's spawn-task tool), also offer the
     successor through it, with that prompt and working directory, so starting it is one click.
5. End your turn. The successor owns the work from here.

## Workers

A worker never starts its own successor: its orchestrator restarts it. On the notice, an
implementer or fixer commits its work as it stands (`WIP: <what is done> (#<N>)`) and ends its
turn with a reply whose last line is `HANDOFF <worktree path> — <what is done, what is next>`.
The orchestrator then starts a fresh worker in the same worktree (orchestrator.md, loop step 7).
A flow checker replies with what it has walked and `FLOWS PARTIAL`.

## At the end of Phase A

Write the hand-off with role `orchestrator`: feature issue, branch, spec path, plan comment URL,
and **Next action** "Startup, from `prompts/orchestrator.md`". Then return to the **Launch** step of Phase A.

## The successor

A successor reads the hand-off first, then the files it points at, and continues from its
**Next action**. An orchestrator successor also reads the state file and picks up each ticket by
its `status`; its workers' sessions survive on disk, so `CX send` reaches them:
- `waiting`, `closed`, `parked`: as usual.
- `question`: answer it (loop step 4).
- `running`: its worker was stopped with the old session; loop step 7.
- `done` or `review`: loop step 5 from the start.
- a `merge` sha recorded but the ticket not `closed`: finish loop step 5.4 (close the issue,
  remove the label, the worktree and the branch).

An audit orchestrator successor continues at its state file's `stage` (audit.md, Startup).
