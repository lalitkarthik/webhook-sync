# Design — the whole product, before any feature

Decides what the product is and writes it down in `docs/design/`: an overview, one page per part,
every question with its answer, the decisions that are hard to change, and the list of every
feature in build order. Runs with the user present, on a `docs/design-<yyyymmdd>` branch off
`main`, and ends with a PR for the user to merge. The context and git rules are in
[../workflow.md](../workflow.md). Every page is written in the format of
[docs-style.md](docs-style.md): read it before writing the first one.

Commit on the branch after every round and every page, so a hand-off loses nothing. A design
session has no feature issue: its hand-off file has role `design` and the product name as its
`<slug>` (lowercase letters, digits and hyphens), names the product and the
`docs/design-<yyyymmdd>` branch where the format says feature and `feature/<slug>`, and leaves
out the ticket lines. A successor (`/feature-loop design <hand-off path>`) reads the hand-off and
`docs/design/`, then continues from its **Next action**.

## What design does not do

No spec, no tickets, no code, and no `features/` map: that map describes behaviour that is
**built**, and is owned by setup's verify step and the verifier. Design stops at the feature list;
`/feature-loop plan` then takes one feature from it at a time.

## 1. Start

Look before asking anything, and pick the case:

- **`docs/design/` exists** → extend it; never start over. Read `00-overview.md` (or
  `overview.md`), `build-order.md`, `grilling-log.md` and `decisions/`, then ask the user what is
  new or has changed. New rounds are appended to the log, pages are revised in place with an
  *Updated* line, and a decision that flips gets a new record that supersedes the old one. Go to
  step 3, or to step 2 when the product's shape itself is in question.
- **A new product** (no code, or only a scaffold) → go to step 2. A folder that is not a git
  repository yet gets `git init -b main` and an empty first commit before the branch.
- **An existing product with no `docs/design/`** → read `README`, `CONTEXT.md`, the code layout
  (top-level folders, entry points, the main modules), the open issues
  (`gh issue list --state open`) and `docs/`. When the code is too large to read within the
  context budget, give each of several `fl-opus-high` subagents a share and have them return, per
  part: what it does, where it starts and what it depends on. Before asking anything, write down
  what exists: a first `00-overview.md` with Status `built` (the paragraph, the picture and the
  parts table, describing what the code does today), and the top of `build-order.md` with the
  features already built, ticked. Records in `docs/decisions/` move to `docs/design/decisions/`
  with `git mv`, numbers kept and links fixed: once a design exists, that is the decisions folder
  (`docs/agents/domain.md`). Commit, show the user, and correct what they say is wrong. Then go
  to step 2.

## 2. Shape

Write one paragraph: what the product is, who it is for, and what it deliberately is not. Then
offer two or three candidate shapes for the product (for example a command-line tool, a library
with a thin app on top, a hosted service), each with what it makes easy, what it makes hard and
what it costs to change later, and recommend one. For an existing product the shapes are the
directions it could take from here; the shape it has today is one of them. The user picks. The
paragraph and the pick become the first rows of the grilling log.

This is the only step taken from brainstorming. Everything after it is grilling.

## 3. Grill the whole product, in rounds

Follow [grill.md](grill.md) — its tags, INTERNAL questions decided by you, the two plain lines
before each question — over the whole product, not one feature: who uses it and how, the parts
and the edges between them, the data it keeps, what it reads and writes outside itself, privacy
and security, what happens when something fails, how it is tested and proved, and what is out of
scope.

Ask in **rounds**. A round holds only questions whose answers do not depend on anything still
open; a question that waits on another answer goes into a later round. After each round:

1. **Log it.** Append to `docs/design/grilling-log.md` a heading `## Round <n>: <topic>` and one
   table, `# | Question | Decision | Why`. Every question is a row, whoever decided it: the Why of
   an INTERNAL decision ends "(decided internally)", and of a recommendation the user accepted
   without comment "(recommendation accepted)". These rows stand in for grill.md's Assumptions
   list; design writes no separate one. `Q#` runs on across rounds and is never renumbered. A
   changed answer keeps its row, marked *Superseded by Q<n>*, and the new answer takes a new
   `Q#`. The log opens with a `flowchart LR` holding one node per round; add this round's node.
2. **Terms.** Add each new term to `CONTEXT.md`: `**Term** — definition in one sentence.`
3. **Decisions.** Write each HARD-TO-CHANGE decision as a record,
   `docs/design/decisions/NNNN-<slug>.md`, numbered on from the last one, and link it from its
   row's Why.
4. **Commit.**

The grill is done when the frontier is empty and the user confirms shared understanding.

## 4. Research

List what the grill could not settle: how others built this, the risks and tricks that matter,
and any fact about an outside system that a decision hangs on. Run the **Research** step of
[../workflow.md](../workflow.md) Phase A on each topic (the same `cx pick` rule), saving one note
per topic as `docs/research/<topic>.md`, sources cited. Then grill again on the findings: new
rounds, logged as in step 3, until the frontier is empty.

## 5. Write the design pages

`docs/design/00-overview.md`, in this order:

- the opening lines and **Status.**;
- **What it is, in one paragraph**;
- **The whole product, in one picture**: one mermaid flowchart holding every part;
- **The parts, one at a time**: the parts table;
- **Rules every part follows**: a numbered list, each rule linked to its record or `Q#`;
- **Reading order**: `CONTEXT.md`, this page, the part pages in the order of the picture,
  `build-order.md`, `decisions/`, `grilling-log.md`, `research-gaps.md`, the research notes.

Then one page per part, `docs/design/<part>.md`: the opening lines and **Status.**; a diagram of
what happens inside the part; its rules in a table, each stating the decision and linking its
`Q#` or record; and **What it deliberately does not do**. For an existing product, a part that
exists has Status `built` and describes what the code does, checked by reading it; anything
planned for it is marked as planned.

A page holds only what the log and the records decided. Something you find undecided while
writing goes back to step 3 as a question; it never goes onto the page as a guess.

## 6. The feature list — `docs/design/build-order.md`

This page answers "what are all the features in this product", and it is what `plan`'s Slice
mode reads to propose the next feature.

- **Features**: one table, `# | Feature | Contains | Done when`, in build order, so a feature
  depends only on rows above it. Above the table, a `flowchart LR` of the features with an arrow
  for each such dependency.
- Each row is sized to be one plan loop and one PR: one thing a user can see working, small
  enough that its PR stays under the size `scripts/claude/risk_tier.py` holds a PR at
  (`pr_max_lines` and `pr_max_files`; 2,000 changed lines and 60 files by default). Split a row
  that is larger; merge a row with nothing a user can check into its neighbour. The first unbuilt
  row is the thinnest slice that runs end to end.
- **Done when** is something a user can observe, never "the code is written".
- For an existing product the features already built come first, with `✓` in the `#` column and
  "Merged" (and the PR, when known) under Done when; the unbuilt ones are numbered from 1.
- **Parked, built on demand**: a second table, `Item | Built when | Design`, for what is designed
  but not scheduled, with what would trigger building it and the page that holds its design.
- Check the list against the pages: every part and every behaviour they describe is in some
  row's **Contains** or on the parked list.

## 7. Gaps

Run [gaps.md](gaps.md) on the new design, on this branch and without a PR of its own (from its
step 2 when this session wrote the pages, from step 1 otherwise), so `research-gaps.md` joins the
design PR. Like every design page it opens with the **What this page contains.** and **How to
read it.** lines; the template in gaps.md has them. When the context guard has already warned
(35%), skip this step and tell the user in the final message to run `/feature-loop gaps` before
the first `/feature-loop plan`.

## 8. Approve and hand over

Show the user, in one message: the link to `docs/design/00-overview.md`, the feature table from
`build-order.md`, and the open RESEARCH and SPIKE gaps if step 7 ran. Ask for one approval; on
changes, go back to the step they touch. On approval, commit, `git push -u origin
docs/design-<yyyymmdd>` and open the PR `Design: <product>`, its body listing the pages, the
rounds and `Q#` range, the decision records and the number of features. Without a GitHub remote,
commit and say that the branch is ready for the user to merge into `main`.

Final message: the PR link, and the next step once it is merged — `/feature-loop plan`, which
will propose the first unbuilt build-order feature. Name `/feature-loop setup` before it when
`CLAUDE.md` has no `feature-loop` section yet.
