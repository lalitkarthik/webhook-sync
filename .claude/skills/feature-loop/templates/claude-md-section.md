<!-- feature-loop:start -->
## Feature workflow

Changes that add or alter behaviour go through the feature loop: `/feature-loop plan` (it starts
the build itself when you approve), and `/feature-loop audit` checks the built product.
Documentation-only changes and one-line fixes take a branch and a PR with no spec. Phases, the
wave rule, the models, the context budget (hand off at 40%, never past 45%) and the git rules are
in `.claude/skills/feature-loop/workflow.md`. Gate scripts live in `scripts/claude/`, configured
by `scripts/claude/fl-config.json`; `main` is changed only by `scripts/claude/automerge.py` or by
you.

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
| Run the app | `{{RUN_APP}}` |
| App URL | `{{APP_URL}}` |

### Docs

| Path | Holds | Written when |
| --- | --- | --- |
| `CONTEXT.md` | the glossary: one line per term | the moment a term is first used |
| `docs/design/` | the project design, when there is one: overview, part pages, grilling log, and `build-order.md` (every feature, in build order); the source of truth it covers | `/feature-loop design`; `/feature-loop gaps` |
| `docs/decisions/NNNN-<slug>.md` | one decision record each (in `docs/design/decisions/` when there is a design) | when a decision is locked in |
| `docs/specs/<slug>.md` | one spec per feature, with its Assumptions and a `Status:` line | planning; status updated at the build's end |
| `docs/research/<topic>.md` | research notes with sources | planning; `/feature-loop gaps` |
| `docs/audits/<yyyymmdd>.md` | one ledger per audit run: every finding and its status | `/feature-loop audit` |
| `features/` | the feature map: how to reach and drive each feature as a user | setup; updated after each feature |
| `.claude/skills/verify-<app>/` | how to launch, drive and observe the app | setup; maintained after each feature |
| `docs/agents/findings.tsv` | every review and verifier finding, by category | each build |
| `docs/agents/metrics.md` | one line per feature: first-pass rate per worker label, escalations | each build's end |
| `CHANGELOG.md` | what changed for the user, newest first | the end of a build or an audit |

Reading rules for these files are in `docs/agents/domain.md`. Design pages, specs and research
notes are written in the house format of `.claude/skills/feature-loop/prompts/docs-style.md`.
<!-- feature-loop:end -->
