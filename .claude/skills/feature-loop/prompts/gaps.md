# Gaps — unchecked outside facts in the design

Finds every design decision that rests on a fact about an outside system nobody has checked,
writes them to `docs/design/research-gaps.md`, then closes them through research. Runs with the
user present, on a `docs/gaps-<yyyymmdd>` branch off `main`, and ends with a PR for the user to
merge.

## 1. Read the design

Read `docs/design/`: every page, `decisions/`, `grilling-log.md`, `audit/`, and every research
note the design links to (usually `docs/research/`). When the design is too large to read within
the context budget, give each of several `fl-opus-high` subagents a share of the files and have
them return candidate gaps in the table format below; merge and de-duplicate their lists.

## 2. Find the gaps

A **gap** is a decision (a `Q#` in the grilling log, or a decision record) that holds only if a
fact about an outside system is true:

- an API's behaviour or limits (rate limits, payload sizes, auth, ordering, uptime)
- a library's capability or licence
- a file or wire format
- pricing or terms of service

For each gap, record:

- **Decision** — the `Q#` or decision record number.
- **Fact assumed** — one sentence, specific enough to verify.
- **Breaks if wrong** — what has to change, and whether the architecture breaks (the change
  spreads beyond one slice or overturns a decision record) or stays inside one slice.
- **First slice** — the earliest build-order step that depends on it.
- **Check** — `OK` when the design or a linked note already cites a source that confirms it
  (name the source); `RESEARCH` when documentation or other primary sources can settle it;
  `SPIKE` when only running code against the real system can.

Done when every `Q#` and every decision record has been scanned and each outside fact it rests
on is in the table.

## 3. Write `docs/design/research-gaps.md`

Rank architecture-breaking gaps first, then by earliest first slice.

```markdown
# Research gaps

Scanned: Q1–Q47, decisions 0001–0012. Updated: <yyyy-mm-dd>.

**What this page contains.** Every design decision that holds only if a fact about an outside
system is true, and whether that fact has been checked.

**How to read it.** Gaps that would break the architecture come first, then the rest by the
earliest build-order step that depends on them. **Check:** `OK` names the source that confirms
the fact, `RESEARCH` can be settled from documentation, `SPIKE` needs code run against the real
system.

| # | Decision | Fact assumed | Breaks if wrong | Arch | First slice | Check | Status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| G1 | 0004 | The market-data API allows 20 requests/s per key | polling design; switch to streaming | yes | 2 | RESEARCH | open |
| G2 | Q12 | The PDF library is MIT-licensed | pick another library | no | 1 | OK — docs/research/pdf.md | closed |
```

Commit it, then show the user the open RESEARCH and SPIKE rows.

## 4. Offer research

Group the top open RESEARCH gaps into calls of 3–5 related gaps (same system or same question
area) and offer to run them. For each group the user accepts, invoke
`/mp-research` with the gap ids and each fact to verify, asking for a verdict per
gap — confirmed, contradicted or unclear — with its primary source. Save each note to
`docs/research/<topic>.md`. List the SPIKE gaps as candidates for `/mp-prototype`.

## 5. Close the gaps

When the research notes are in, handle every gap they cover:

- **Confirmed** → Check becomes `OK — <note path>`, Status `closed`.
- **Contradicted** → grill the user on it with the tag rule in [grill.md](grill.md), only for
  these findings. Update the design pages and decision records the answer changes (a decision
  that flips gets a new record that supersedes the old one), then mark the gap `closed` with
  the new record's number.
- **Unclear** → stays `open`; change Check to `SPIKE` when only running code can settle it.

Done when every researched gap is closed or carries its new Check, and `research-gaps.md`,
the design pages and the decision records are committed together.
