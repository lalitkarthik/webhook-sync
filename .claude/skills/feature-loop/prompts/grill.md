# Grill

Invoke `/mp-grilling` and run it as written, with the three additions below. On the
second pass (after research) keep the same Assumptions list and continue its numbering.

## 1. Tag every question

Before a question goes into a round, give it exactly one tag:

- **CLIENT** — decides what the user or client sees, gets, or pays for: scope, behaviour,
  wording, priorities.
- **HARD-TO-CHANGE** — expensive to reverse once built: data model, stored formats, public APIs,
  external contracts, security and permissions, the choice of a dependency or service.
- **INTERNAL** — an implementation choice the client never sees and a later ticket can change
  cheaply: naming, module layout, a library for one job, test structure.

A question that fits both CLIENT and HARD-TO-CHANGE takes HARD-TO-CHANGE.

## 2. Decide INTERNAL questions yourself

Settle each INTERNAL question with the choice best supported by the codebase, the research and
the decision records. Below each round's questions, list what you settled under
**Decided internally**, one line each (decision — reason), so the user can overrule any of them.

## 3. Explain before asking

CLIENT and HARD-TO-CHANGE questions go to the user. Each opens with two plain lines: what the
answer decides, and what getting it wrong costs later. Then the grilling format:

```
❓ **Q3** [HARD-TO-CHANGE] — **Where order history is stored**
Decides whether history lives in the main database or a separate log.
Moving it after launch means migrating every stored order.

<question body, choices if any>

➡️ <recommended answer>
```

## Done

The grill is done when the frontier is empty and the user confirms shared understanding. Close
with the **Assumptions** list: every INTERNAL decision, every recommended answer the user
accepted without comment, and everything taken as given without being asked.

```
## Assumptions
- A1 [INTERNAL] Orders are validated in the service layer — keeps handlers thin.
- A2 [CLIENT] Only logged-in users see history — the user accepted the recommendation.
```

This list goes into the spec unchanged (the **Spec** step).

## Slice mode — when `docs/design/` exists

The design is the source of truth; this grill covers only what it leaves open for one slice.

1. **Read the design**: `docs/design/00-overview.md` (or `overview.md`; either name is the
   overview), `build-order.md`, `grilling-log.md`, the design's `decisions/`, and
   `research-gaps.md` if present.
2. **Pick the slice.** Ask which build-order step or slice this feature is. Recommend the
   earliest step that is not ticked as built and has no spec in `docs/specs/` and no `feature`
   issue yet.
3. **Check open gaps.** If `research-gaps.md` lists an open RESEARCH or SPIKE gap whose first
   slice is this one or earlier, name it and recommend `/feature-loop gaps` before going on.
4. **Grill the gaps.** List what this slice needs decided that the design does not settle:
   behaviour it leaves unspecified, choices it defers, edges between this slice and the next.
   Grill only those, with the same tags, INTERNAL decisions and two-line explanations as above.
   Cite the `Q#` or decision record that already covers anything the user raises that the
   design settles, and move on.
5. **Done** as above, with the Assumptions list. Decisions locked in here go to the design's
   `decisions/` folder; the grill moves on to the **Spec** step.
