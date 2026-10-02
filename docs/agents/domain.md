# Domain docs

How the engineering skills read and write this repo's domain documentation. The repo is
**single-context**.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root — the glossary, one line per term. It is the source of truth
  for the project's vocabulary.
- **`docs/design/`**, when it exists — the project design: `00-overview.md`, `build-order.md`,
  `grilling-log.md` (numbered `Q#` decisions), `decisions/`, `research-gaps.md`, `audit/`. It is
  the source of truth for everything it covers; specs link to it rather than restating it.
- **The decisions folder** — `docs/design/decisions/` when `docs/design/` exists, otherwise
  `docs/decisions/`. Records are named `NNNN-<slug>.md`. Read the ones that touch the area you
  are about to work in.
- **`docs/specs/<slug>.md`** — the spec of the feature you are working on, with its Assumptions.
- **`docs/research/`** — research notes, each citing its sources.

A missing file means nothing has been recorded yet: carry on without it. `/domain-modeling`
creates entries when a term or decision is actually resolved.

## Where `/domain-modeling` writes

- A new or sharpened **term** → one line in `CONTEXT.md`: `**Term** — definition in one sentence.`
- A new **decision** → `NNNN-<slug>.md` in the decisions folder, numbered on from the last
  record: context, decision, consequences, each in a few plain sentences. A decision that
  reverses an earlier one supersedes it and says so in both records. (This repo never uses
  `docs/adr/`.)

## Use the glossary's vocabulary

When your output names a domain concept (an issue title, a test name, a proposal), use the term
exactly as `CONTEXT.md` defines it. A concept missing from the glossary is a signal: either the
project does not use that language, or there is a gap to record.

## Flag decision conflicts

When your output contradicts a decision record, say so explicitly:

> _Contradicts decision 0003 (orders are immutable) — worth reopening because…_
