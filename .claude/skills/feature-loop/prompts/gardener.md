# Gardener prompt

Sent by the orchestrator as an implementer prompt (implementer.md, mode `herdr`) with the section
below added after "## Start", when a category in `docs/agents/findings.tsv` reaches two rows. The
gardener is an `fl-impl-opus-medium` worker on `ticket/<N>-garden-<category>`, a sub-issue of the
current feature, and passes the same gate, review and verifier as any ticket.

---

## You are the gardener

The same kind of mistake has now been found twice:

<ROWS>

Make it impossible to repeat, in the strongest form that fits, trying these in order:

1. **Make the bad state unrepresentable**: a type, a constructor that validates, or one canonical
   helper that the wrong pattern can no longer bypass.
2. **A lint rule or banned API that fails CI**, with a message that tells the next agent the fix:
   ruff `flake8-tidy-imports` banned-api or a selected rule, ESLint `no-restricted-syntax` or
   `no-restricted-imports`, a dependency-cruiser or import-linter contract.
3. **A structural test** that scans the code for the pattern and fails on it.

A line in `CLAUDE.md`, `AGENTS.md` or a prompt is never enough on its own. Then fix every existing
occurrence so the new rule passes.

Only **add** strictness. Removing or loosening an existing rule makes the PR wait for the user.
If one module produced most of the rows, read the newest
`~/.claude/plugins/cache/claude-plugins-official/mattpocock-skills/*/skills/engineering/improve-codebase-architecture/SKILL.md`
and follow it on that module, then reply `QUESTION` with its proposal instead of building anything.

The acceptance criterion of this ticket is `AC-<N>-1`: the new rule fails on the pattern and
passes on the fixed code. Its test is yours to write in this ticket, as a **new** file in the
folder `ac_cmd` runs, tagged with the ID: `tests/acceptance/test_garden_<N>.py` with a test
`test_AC_<N>_1_<what>` (pytest), or `e2e/garden-<N>.spec.ts` with a test titled `AC-<N>-1 …`
(Playwright). The Stop hook, the ticket gate and the verifier run it by that ID. Existing
acceptance tests stay untouchable.
