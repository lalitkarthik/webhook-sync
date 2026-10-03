# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues in `{{REPO}}`. Use the `gh` CLI for every
operation; it infers the repo from `git remote -v`, so run it inside a clone or worktree.

## Conventions

- **Create**: `gh issue create --title "..." --body-file <file>` (write multi-line bodies to a file first).
- **Read**: `gh issue view <number> --comments`.
- **List**: `gh issue list --state open --label ticket --json number,title,labels --jq '...'`.
- **Comment**: `gh issue comment <number> --body "..."`.
- **Labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`.
- **Close**: `gh issue close <number> --comment "..."`.

## Two kinds of issue

| Kind | Label | Created by | Body |
| --- | --- | --- | --- |
| Feature | `feature` | `/mp-to-spec` | Plain-language summary, link to `docs/specs/<slug>.md`, the spec, its Assumptions |
| Ticket | `ticket` | `/mp-to-tickets` | Plain-language summary, then the ticket template |

A feature issue also carries `ready-for-agent`. A ticket carries `ready-for-agent` when created,
`in-progress` while an implementer works on it, and is closed once merged into the feature branch.

## Tickets are sub-issues of their feature

The API needs numeric database ids, not `#numbers`:

```bash
OWNER_REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
TICKET_DB_ID=$(gh api "repos/$OWNER_REPO/issues/<ticket>" --jq .id)
gh api --method POST "repos/$OWNER_REPO/issues/<feature>/sub_issues" -F sub_issue_id="$TICKET_DB_ID"
```

Also open each ticket body with `## Parent` and `Part of #<feature>`, so the link survives
without sub-issues.

## Blocking edges

Native issue dependencies; the orchestrator reads them to find the frontier.

```bash
BLOCKER_DB_ID=$(gh api "repos/$OWNER_REPO/issues/<blocker>" --jq .id)
gh api --method POST "repos/$OWNER_REPO/issues/<blocked>/dependencies/blocked_by" -F issue_id="$BLOCKER_DB_ID"
```

Keep a `## Blocked by` section in the ticket body too: `#<n>` per blocker, or
"None — can start immediately". A ticket is unblocked once every blocker is **closed**.

**Frontier query** — open tickets of the feature with no open blocker, not `in-progress` and not
parked (`ready-for-human`, `needs-info`):

```bash
gh api "repos/$OWNER_REPO/issues/<feature>/sub_issues" \
  --jq '.[] | select(.state=="open") | select((.issue_dependencies_summary.blocked_by // 0)==0) | select([.labels[].name] | any(. == "in-progress" or . == "ready-for-human" or . == "needs-info") | not) | "#\(.number) \(.title)"'
```

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Set to `yes` if this repo treats external PRs as feature
requests; `/triage` reads this flag.)_

Issues and PRs share one number space: resolve a bare `#42` with `gh pr view 42`, falling back
to `gh issue view 42`.

## When a skill says "publish to the issue tracker"

Create a GitHub issue as above. A feature spec is also written to `docs/specs/<slug>.md`.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.
