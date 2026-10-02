# Hand-off file format

Written when a session reaches 60% of its context window, and at the end of planning. The
procedure is `.claude/skills/feature-loop/prompts/handoff.md`. Save it outside the repo as
`<temp dir>/feature-loop-handoff-<role>-<slug>-<yyyymmdd-hhmm>.md`. Redact secrets. Link to
issues, specs, commits and diffs instead of repeating them.

```markdown
# Hand-off: <role> for feature #<n> (<Feature name>)

**Predecessor:** <session or Herdr agent name; Herdr tab id to close first, if any>
**Branch / cwd:** `feature/<slug>` in `<path>`
**State file:** `<temp dir>/feature-loop-<slug>.json` (orchestrator only)
**Route:** default | Herdr

## Where things are
- Feature issue: #<n> (plan comment: <url>)
- Tickets: #<a>, #<b>, …
- Spec: `docs/specs/<slug>.md`; research: `docs/research/<slug>.md`; design: `docs/design/` pages it rests on

## Done so far
- <ticket or step> — <one line>

## In flight
- <ticket> — status `<done|question|running>`, worktree `<path>`, branch `ticket/<n>-<slug>`

## Not started
- <ticket> — blocked by <…> / waiting for wave <k>

## Open questions and problems
- <needs-info or ready-for-human tickets, failing tests, unfixed review findings>

## Next action
The single next thing to do, precisely.

## Suggested skills
- `/mp-code-review`, `/mp-resolving-merge-conflicts`, …
```
