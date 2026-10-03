# webhook-sync

Receives payment events (Razorpay first; Stripe designed but parked), keeps one ledger row per payment, mirrors it
to a Google Sheet, and proves nothing was lost or written twice. Designed, not yet built.

- **Start here:** `docs/design/00-overview.md`. Razorpay is the first source and Stripe is parked
  (grilling round 11, Q103-Q114); `docs/design/razorpay-first-brief.md` is superseded.
- **Words:** `CONTEXT.md`. Use its terms exactly.
- **Cloud sessions:** when `CLAUDE_CODE_REMOTE` is `true`, read `docs/agents/cloud.md` first.
- **Local sessions:** otherwise, read `docs/agents/local.md` first: setup, the smoke test and the
  plan → build → merge → clean-up cycle.
- **Secrets:** environment variables named in `.env.example`; test-mode keys only; never in a
  file in the repo, a commit, an issue or a log.
- **Commits and PRs** carry no attribution lines of any kind.
- **Sub-agents:** run them in parallel wherever the work is independent (research, exploration,
  implementers); none may start agents of its own.
- **Skills** live in `.claude/skills/`: `/feature-loop`, and the `/mp-*` skills it calls.

<!-- feature-loop:start -->
## Feature workflow

Changes that add or alter behaviour go through the feature loop: `/feature-loop plan` (it starts
the build itself when you approve), and `/feature-loop audit` checks the built product.
Documentation-only changes and one-line fixes take a branch and a PR with no spec. Phases, the
wave rule, the models, the context budget (hand off at 40%, never past 45%) and the git rules are
in `.claude/skills/feature-loop/workflow.md`. Gate scripts live in `scripts/claude/`, configured
by `scripts/claude/fl-config.json`; `main` is changed only by `scripts/claude/automerge.py` or by
you.

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
| Run the app | `uv run wsync serve --config webhook-sync.toml` |
| App URL | `http://127.0.0.1:8080/health` |

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

## Agent skills

### Issue tracker

GitHub issues in `lalitkarthik/webhook-sync`, driven with `gh`; a feature is an issue and its tickets are sub-issues. See `docs/agents/issue-tracker.md`.

### Triage labels

The five default roles, each label equal to its name (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`), plus the feature loop's `feature`, `ticket` and `in-progress`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` at the root, decision records in `docs/design/decisions/`. See `docs/agents/domain.md`.
