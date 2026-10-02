<!-- feature-loop:start -->
## Feature workflow

Changes that add or alter behaviour go through the feature loop: `/feature-loop plan`, then
`/feature-loop orchestrate #<feature issue>`. Documentation-only changes and one-line fixes take
a branch and a PR with no spec. Phases, the wave rule, the 60% context budget and the git rules
are in `.claude/skills/feature-loop/workflow.md`.

Issues live on GitHub at `{{REPO}}`, driven with `gh`: a feature is an issue labelled `feature`,
its tickets are sub-issues labelled `ticket` with native blocked-by edges. See
`docs/agents/issue-tracker.md` and `docs/agents/triage-labels.md`.

### Commands

| Step | Command |
| --- | --- |
| Set up a fresh worktree | `{{SETUP_WORKTREE}}` |
| Typecheck | `{{TYPECHECK}}` |
| Lint and format check | `{{LINT}}` |
| Run one test file | `{{ONE_TEST}}` |
| Run the full test suite | `{{FULL_SUITE}}` |

### Docs

| Path | Holds | Written when |
| --- | --- | --- |
| `CONTEXT.md` | the glossary: one line per term | the moment a term is first used |
| `docs/design/` | the project design, when there is one: the source of truth it covers | design work; `/feature-loop gaps` |
| `docs/decisions/NNNN-<slug>.md` | one decision record each (in `docs/design/decisions/` when there is a design) | when a decision is locked in |
| `docs/specs/<slug>.md` | one spec per feature, with its Assumptions and a `Status:` line | planning; status updated at the build's end |
| `docs/research/<topic>.md` | research notes with sources | planning; `/feature-loop gaps` |
| `CHANGELOG.md` | what changed for the user, newest first | the build's end, one entry per feature |

Reading rules for these files are in `docs/agents/domain.md`.
<!-- feature-loop:end -->
