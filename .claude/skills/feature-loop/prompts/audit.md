# Audit orchestrator — Phase D

You are the audit orchestrator. You run unattended. Each **round**, auditors find where the
product breaks its promises, you triage what they found, fixers repair what is above the fix
line, and you verify. Rounds repeat until one finds nothing new to fix. The rules you apply —
the fix line, models, context budget, git — are in [../workflow.md](../workflow.md).

The loop stays inside the product as specified: it fixes broken promises and never builds an
idea.

**Limits:** 20 fix tickets per round, 2 rounds.

**Never end your turn while auditors or fixers run:** a successor runs headless (`claude -p`),
where ending the turn ends the session. At the 40% notice follow [handoff.md](handoff.md), which
starts that successor itself.

**Routes.** Auditors start as foreground `Agent` calls (`fl-opus-high`), or [../herdr.md](../herdr.md) when `HERDR_ENV=1`. When `node ~/.claude/accounts/cx.mjs pick` names an extra account, start an auditor on it instead: add its worktree with `git worktree add <path> -b audit-r<round>-<lens> <BASE>`, save the filled [auditor.md](auditor.md) as a prompt file, and run `node ~/.claude/accounts/cx.mjs run --name audit-r<round>-<lens> --account <account> --cwd <path> --agent fl-opus-high --model opus --effort high --prompt-file <file>` with the Bash tool and `run_in_background: true`; record the start time in ms and wait with `cx wait --since <ms> <names>` instead of ending your turn. Fixers start exactly like implementers in [orchestrator.md](orchestrator.md) (`cx run --model opus --effort medium`), and each fix ticket passes the Loop's test and review.

## Startup

1. **Resume first.** If `TMP/feature-loop-audit-<date>.json` or a
   `TMP/feature-loop-handoff-audit-*.md` exists, read it and continue at its `stage`.
2. **Scope and target.** With no argument the scope is the whole product and the target is
   `main`. With `#<feature>` the scope is that feature, and the target is `feature/<slug>` while
   its PR is open, `main` once merged. Check out the target, `git pull`, and check `git status --short`
   prints nothing and the `CLAUDE.md` section has no `{{` left; otherwise report and stop.
3. **Branch.** `git checkout -b audit/<yyyymmdd>` (add `-2`, `-3` if taken). This is `<BASE>`
   for every auditor and fixer.
4. **Reading list.** Whole product: `docs/design/` when present, every `docs/specs/*.md` whose
   status is `implemented`, `CHANGELOG.md`. One feature: its spec, the design pages the spec
   links, its feature issue and tickets.
5. **Parent issue.** `gh issue create --title "Audit <yyyy-mm-dd>" --label feature --label audit`
   with a two-line body naming the scope.
6. **Ledger.** Create `docs/audits/<yyyymmdd>.md` in the format below. Carry in every open
   issue labelled `audit` from earlier runs: those also labelled `ready-for-agent` are
   `over-cap` and go to the front of round 1's fix list; the rest are `minor`. Commit.
7. **State file.** `TMP/feature-loop-audit-<date>.json`: `target`, `branch`, `parent`, `round`,
   `stage` (`audit`, `triage`, `fix`, `verify`), the auditors and the tickets, kept current
   after every change.

## The round

1. **Audit.** For each lens — `spec`, `tests`, `flows`, `failures` — fill
   [auditor.md](auditor.md) and call `Agent` with `subagent_type: "fl-opus-high"`,
   `isolation: "worktree"`, findings file `TMP/feature-loop-audit-<date>-r<round>-<lens>.md`,
   all in one message and without `run_in_background`: they run in parallel and the message
   returns when every one is done. Skip `flows` when the section's "Run the app" is `none`, and
   note it in the ledger. While Docker is running, auditors that run the app or the suite go one
   at a time. Each auditor's last line is `DONE <findings file> <worktree>`. When all are in,
   discard their worktrees
   (`git worktree remove --force <path>`, then delete its branch).
2. **Triage.** Read each findings file once.
   1. Merge findings that share a cause, keeping the best reproduction; drop any already in
      the ledger.
   2. Re-run every reproduction that is a single command, output capped. One that no longer
      fails makes its finding a suspicion. Test and playwright reproductions are confirmed
      later by the fixer's first red test.
   3. Findings marked `Research: yes`: invoke `/mp-research` once for the round,
      3–5 facts per call, saved to `docs/research/audit-<yyyymmdd>.md`, then sort them with the
      answer in hand.
   4. Sort every finding with the fix line. The auditor's `Line` and `Tag` are proposals; the
      decision is yours.

      | Finding | Outcome | Ledger status |
      | --- | --- | --- |
      | Above the line | fix ticket, at most 20 this round: over-cap from before first, then security and data loss, crashes, wrong results, blocked flows | `fixed` once closed |
      | Above the line, past the 20 | issue labelled `audit` and `ready-for-agent` | `over-cap` |
      | CLIENT or HARD-TO-CHANGE choice | issue labelled `audit` and `needs-info`, opening with the two-line explanation and the question | `parked` |
      | Minor | issue labelled `audit` | `minor` |
      | No working reproduction | none | `suspicion` |
      | Breaks no promise | none | `idea` |

      An INTERNAL choice you settle goes under **Assumptions** in the ledger.
   5. Write each fix ticket: labels `ticket`, `audit`, `ready-for-agent`; a sub-issue of the
      parent; body with `## Parent`, a plain-language summary, `## Breaks`, `## Reproduction`,
      `## Suggested change`, `## Blocked by`. Place the round's tickets into waves by their
      Areas with the wave rule, and post the table on the parent issue as
      `## Implementation plan — round <r>`.
   6. Update the ledger and commit it.
3. **Fix.** Run the **Loop** of [orchestrator.md](orchestrator.md) on this round's fix
   tickets, reading `feature/<slug>` as `audit/<yyyymmdd>` and "the spec" as the document each
   ticket's `## Breaks` cites. Every fixer is Opus medium. A fixer's `QUESTION` saying
   the reproduction passes closes the ticket as not reproducible (`suspicion`). With no fix
   tickets, skip to step 5.
4. **Verify.** Reproductions that became tests are verified by the suite the Loop already ran
   green. For fixed findings whose reproduction is playwright steps, start one `fl-opus-high`
   foreground `Agent` in a worktree on `<BASE>` to start the app, walk each one, and reply `PASS`
   or `FAIL` per finding. A `FAIL` reopens its ticket for one more fixer round; a second `FAIL`
   swaps its labels to `ready-for-human` and its ledger status to `parked`.
5. **Converge.** The round is **clean** when triage produced no fix ticket and no over-cap
   finding. Clean → Finish, converged. Round 2 and not clean → Finish, not converged.
   Otherwise start the next round with fresh auditors and the updated ledger.

## The ledger

`docs/audits/<yyyymmdd>.md`, committed on the audit branch after every triage and every closed
ticket. Each finding ever reported appears once.

```markdown
# Audit <yyyy-mm-dd> — <whole product | feature #<n>>

Result: <running | converged in round <r> | not converged after 2 rounds>

| # | Round | Lens | Finding | Breaks | Status | Issue |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | 1 | flows | Double submit charges twice | specs/checkout.md "one charge per order" | fixed | #41 |

## Assumptions
- A4: empty search returns all results — matches the list page's default.

## Round 1
Lenses: spec, tests, flows, failures. Skipped: none.
Coverage: <each auditor's Coverage line>
Ideas: <one line each>
Suspicions: <one line each>
```

## Finish

1. **Ledger.** Set `Result`. For "not converged", list what round 2 still found.
2. **Changelog.** Add `## Audit <yyyy-mm-dd>` at the top of `CHANGELOG.md` with one plain line
   per fix the user would notice. Commit.
3. **PR.** `git push -u origin audit/<yyyymmdd>`, then `gh pr create` against the target with:
   the result, the fixed findings, the parked ones and why, the counts of minor findings,
   suspicions and ideas, a link to the ledger, and `Closes #<parent>`. Comment the PR link on
   the parent issue.
4. **Report** in your session: converged or not and in which round, what was fixed, every
   issue waiting on the user (`needs-info`, `ready-for-human`), and any lens that was skipped.
   Send it as a push notification too when that tool exists.
