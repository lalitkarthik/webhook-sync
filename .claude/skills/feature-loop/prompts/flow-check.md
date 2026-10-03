# Flow-check prompt

The orchestrator fills the `<…>` slots (commands come from the project's `CLAUDE.md` section) and
sends everything below the line as the prompt of a `cx run` worker on Opus medium, in the main
checkout on `feature/<slug>`. It runs once per feature, after every ticket is merged into the
feature branch and before the PR. `<ONLY>` is empty on the first run; on a re-run it lists the
flows to walk again.

---

You are checking feature #<FEATURE> (`docs/specs/<SLUG>.md`) on branch `feature/<SLUG>` in
`<ROOT>`, the way a user meets it. You did not write this code. You change no file in the repo and
you fix nothing: you run the product, watch what happens and report.

## What to walk

<ONLY>

When the line above is empty, list the flows yourself before you start anything:

1. **New flows** — everything the spec says a user can now do, each from its first step to its end
   result: the happy path, then the failure paths the spec itself promises (invalid input, an
   empty state, a refused action).
2. **Touched flows** — every flow that existed before this feature and passes through code it
   changed. `git diff --stat main...HEAD` shows the changed areas; the specs in `docs/specs/`
   with `Status: implemented`, and the `docs/design/` pages when there are any, say which flows
   run through them. Walk each one's happy path once.

At most 12 flows; with more, keep the ones closest to the changed code and name the rest in your
reply as `NOT WALKED`.

## How

- Run `<SETUP_WORKTREE>` if the checkout is not installed. Start the product with `<RUN_APP>` on a
  free port (base URL `<APP_URL>`), using only test accounts and data from the project's seed or
  fixture files.
- With a web UI, invoke the `playwright-cli` skill and drive the pages as a user would: click,
  type, submit, read what the page shows. With no web UI (`<RUN_APP>` is `none`), run the commands
  or call the endpoints the spec names, with the inputs a user would give.
- A flow passes only when you saw its end result: the page, output, file or response the spec
  promises. A passing test suite is not a flow.
- A flow that fails is run once more from a clean start. Failing again, it is a `FAIL`; passing
  the second time, it is a `FAIL` too, with "flaky" in its line.
- Stop every process you started. Screenshots and scratch files go in the temp dir, never in the
  repo: `git status --short` prints nothing when you finish.

## Reply

At most 25 lines. One line per flow: `PASS <flow>` or `FAIL <flow>`; under each `FAIL`, the exact
steps, then expected against actual. The **last line** is one of:

```
FLOWS PASS
FLOWS FAIL
FLOWS PARTIAL
```

`FLOWS PARTIAL` is only for the context guard's "hand off now" notice: stop walking, list the
flows you did not reach as `SKIPPED <flow>`, and reply. A `FAIL` among the flows you did walk makes
the last line `FLOWS FAIL` instead.
