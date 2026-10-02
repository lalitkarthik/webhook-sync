# Hand off

Used at the end of Phase A (role `orchestrator`) and whenever the context guard says "hand off
now" (your own role). The file format is the project's `docs/agents/handoff.md`; the file is
`TMP/feature-loop-handoff-<role>-<slug>-<yyyymmdd-hhmm>.md`.

## On the 60% notice

1. Finish the tool call in progress. From here on, start no new ticket, review or research.
2. **Orchestrator only:** let running implementers reach their `DONE` or `QUESTION` (end your
   turn and take the notifications as they come, doing nothing else with them), then record
   each in the state file as `done` or `question` with its worktree path.
3. Write the hand-off file. Its **Next action** is one precise instruction.
4. Hand over to the successor:
   - default: tell the user to open a fresh session in this checkout and type
     `/feature-loop plan <hand-off path>` (planning) or `/feature-loop orchestrate #<feature>`
     (orchestrator), and name the hand-off path. Send it as a push notification too when that
     tool exists, since the orchestrator runs unattended.
   - `HERDR_ENV=1`: start the successor with the recipe in [../herdr.md](../herdr.md) and
     confirm it is running.
5. End your turn. The successor owns the work from here.

## At the end of Phase A

Write the hand-off with role `orchestrator`: feature issue, branch, spec path, plan comment URL,
and **Next action** "Startup, from `prompts/orchestrator.md`". Then return to the **Hand off** step of Phase A.

## The successor

A successor reads the hand-off first, then the files it points at, and continues from its
**Next action**. An orchestrator successor also reads the state file: a ticket marked `done` goes
to review (loop step 5); one marked `running` or `question` belonged to agents of the old
session, so it restarts as a **resume**-mode implementer (loop step 7), with any known answer
added to its prompt.
