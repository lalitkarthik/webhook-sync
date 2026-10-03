# Waves — the implementation plan

Turns the feature's tickets into waves under the wave rule in [../workflow.md](../workflow.md).

1. **List the tickets.** `gh api "repos/<REPO>/issues/<feature>/sub_issues" --jq '.[] | "#\(.number) \(.title)"'`,
   then read each with `gh issue view <n>`.
2. **Name each ticket's areas** — the modules or directories it will touch. Ground them in the
   real layout: a subagent that maps the code to areas keeps the file dumps out of your context.
   A ticket that creates a new module names that module's future path.
3. **Place the tickets.** Apply the wave rule. Where two unblocked tickets share an area, add a
   blocking edge (the command is in `docs/agents/issue-tracker.md`) and update the blocked
   ticket's `## Blocked by` section, or move one ticket to the next wave.
4. **Post the plan** as a comment on the feature issue:

   ```markdown
   ## Implementation plan

   Parallelism cap: none (1 implementer while Docker runs).

   | Wave | Ticket | Worker | Areas | ACs | Blocked by |
   | --- | --- | --- | --- | --- | --- |
   | 1 | #12 Add order model | opus-medium | `src/orders/` | 3 | — |
   | 1 | #13 Add price feed | sonnet-high | `src/feeds/` | 2 | — |
   | 2 | #14 Show order history | opus-high | `src/orders/`, `src/web/` | 4 | #12 |
   ```

5. **Show the user** the table and any edges you added.

Done when every open ticket sits in exactly one wave and carries exactly one `worker:` label, no two tickets in one wave share an area,
and every blocker of a ticket sits in an earlier wave.
