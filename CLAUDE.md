# webhook-sync

Receives payment events (Razorpay first, Stripe second), keeps one ledger row per payment, mirrors it
to a Google Sheet, and proves nothing was lost or written twice. Designed, not yet built.

- **Start here:** `docs/design/00-overview.md`, then `docs/design/razorpay-first-brief.md`, which
  overrides the other design pages on which source comes first until grilling round 11 folds it in.
- **Words:** `CONTEXT.md`. Use its terms exactly.
- **Cloud sessions:** when `CLAUDE_CODE_REMOTE` is `true`, read `docs/agents/cloud.md` first.
- **Secrets:** environment variables named in `.env.example`; test-mode keys only; never in a
  file in the repo, a commit, an issue or a log.
- **Commits and PRs** carry no attribution lines of any kind.
- **Sub-agents:** one at a time, except the orchestrator's implementers (at most 2), and none may
  start agents of its own.
- **Skills** live in `.claude/skills/`: `/feature-loop`, and the `/mp-*` skills it calls.

<!-- feature-loop:start -->
## Feature workflow

Changes that add or alter behaviour go through the feature loop: `/feature-loop plan`, then
`/feature-loop orchestrate #<feature issue>`. Documentation-only changes and one-line fixes take
a branch and a PR with no spec. Phases, the wave rule, the 60% context budget and the git rules
are in `.claude/skills/feature-loop/workflow.md`.

Issues live on GitHub at `lalitkarthik/webhook-sync`, driven with `gh`: a feature is an issue labelled `feature`,
its tickets are sub-issues labelled `ticket` with native blocked-by edges. See
`docs/agents/issue-tracker.md` and `docs/agents/triage-labels.md`.

### Commands

| Step | Command |
| --- | --- |
| Set up a fresh worktree | `uv sync` |
| Typecheck | `uv run pyright` |
| Lint and format check | `uv run ruff check . && uv run ruff format --check .` |
| Run one test file | `uv run pytest <path>` |
| Run the full test suite | `uv run pytest` |

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

## Agent skills

### Issue tracker

GitHub issues in `lalitkarthik/webhook-sync`, driven with `gh`; a feature is an issue and its tickets are sub-issues. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default roles, each label equal to its name (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`), plus the feature loop's `feature`, `ticket` and `in-progress`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` at the root, decision records in `docs/design/decisions/`. See `docs/agents/domain.md`.
