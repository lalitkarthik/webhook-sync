# Issue tracker: GitHub

Issues and specs for this repo live as GitHub issues in `lalitkarthik/webhook-sync`. Use the `gh` CLI for every
operation; it infers the repo from `git remote -v`, so run it inside a clone or worktree.

## Conventions

- **Create**: `gh issue create --title "..." --body-file <file>` (write multi-line bodies to a file first).
- **Read**: `gh issue view <number> --comments`.
- **List**: `gh issue list --state open --label ticket --json number,title,labels --jq '...'`.
- **Comment**: `gh issue comment <number> --body "..."`.
- **Labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`.
- **Close**: `gh issue close <number> --comment "..."`.

## In a cloud session: REST only

Claude Code cloud sessions block GitHub's GraphQL API, and every `gh issue`, `gh pr`, `gh label`
and `gh repo view` command uses it (HTTP 403 "GitHub GraphQL is not available"). `gh api` with
REST paths works; `{owner}/{repo}` is filled in from the git remote. Use these instead, here and
wherever a skill or prompt names the `gh` command on the left:

| Instead of | Run |
| --- | --- |
| `gh repo view --json nameWithOwner --jq .nameWithOwner` | `gh api 'repos/{owner}/{repo}' --jq .full_name` |
| `gh issue create --title "T" --body-file F` (with labels) | `gh api 'repos/{owner}/{repo}/issues' -f title="T" -F body=@F -f 'labels[]=feature' -f 'labels[]=ready-for-agent' --jq .number` |
| `gh issue view N --comments` | `gh api 'repos/{owner}/{repo}/issues/N' --jq '.title, .body'` then `gh api 'repos/{owner}/{repo}/issues/N/comments' --jq '.[] \| "--- \(.user.login) (comment \(.id)):\n\(.body)"'` |
| `gh issue list --state open --label ticket` | `gh api 'repos/{owner}/{repo}/issues?state=open&labels=ticket&per_page=100' --jq '.[] \| select(.pull_request \| not) \| "#\(.number) \(.title)"'` |
| `gh issue comment N --body "B"` | `gh api 'repos/{owner}/{repo}/issues/N/comments' -f body="B" --jq .id` |
| editing one comment (the orchestrator's state) | `gh api -X PATCH 'repos/{owner}/{repo}/issues/comments/<comment id>' -F body=@F` |
| `gh issue edit N --add-label L` | `gh api 'repos/{owner}/{repo}/issues/N/labels' -f 'labels[]=L'` |
| `gh issue edit N --remove-label L` | `gh api -X DELETE 'repos/{owner}/{repo}/issues/N/labels/L'` |
| `gh issue close N --comment "B"` | the comment as above, then `gh api -X PATCH 'repos/{owner}/{repo}/issues/N' -f state=closed -f state_reason=completed` |
| `gh pr create --base main --head H --title T --body-file F` | `gh api 'repos/{owner}/{repo}/pulls' -f base=main -f head=H -f title="T" -F body=@F --jq .html_url` |
| `gh pr view N --json state --jq .state` | `gh api 'repos/{owner}/{repo}/pulls/N' --jq 'if .merged then "MERGED" else (.state \| ascii_upcase) end'` |
| `gh label create L --color C --description D --force` | `gh api 'repos/{owner}/{repo}/labels' -f name=L -f color=C -f description="D"` (a 422 means it exists) |

The sub-issue, blocking-edge and frontier commands below are already REST and work unchanged;
set `OWNER_REPO=lalitkarthik/webhook-sync` instead of calling `gh repo view`.

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

**Frontier query** — open tickets of the feature with no open blocker and no `in-progress`:

```bash
gh api "repos/$OWNER_REPO/issues/<feature>/sub_issues" \
  --jq '.[] | select(.state=="open") | select((.issue_dependencies_summary.blocked_by // 0)==0) | select([.labels[].name] | index("in-progress") | not) | "#\(.number) \(.title)"'
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
