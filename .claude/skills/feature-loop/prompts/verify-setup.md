# Verify setup — `verify-<app>` and the feature map

Run by `/feature-loop setup` step 8, in the main checkout. Adapted from pstack's
create-verification-skill (MIT, Copyright (c) 2026 Lauren Tan). First read
`SKILL_DIR/vendor/pstack/skills/create-verification-skill/SKILL.md` and its `references/`
folder; this file says only where feature-loop differs.

1. **Interview the repo, not the user.** Find every surface a user touches (web pages, CLI
   commands, HTTP endpoints, webhooks, files in and out); how to run it (dev command,
   environment, seed data, ports); how to drive each surface, existing harnesses first
   (Playwright Test, pytest fixtures, Hurl files), then `playwright-cli` for web, the installed
   entry point as a subprocess for a CLI, `hurl` or `curl` for HTTP; what can be observed; and
   whether two instances can run side by side.
   If the built-in `run-skill-generator` skill is available, invoke it first: it records the
   launch recipe in `.claude/skills/run-<app>/`, and the Launch section below reuses it.
2. **Isolation.** Every instance takes its port from `python scripts/claude/port.py`, keeps its
   data under `.verify/`, and writes its server PID to `.verify/pid`. Cleanup kills that PID
   tree (`taskkill //T //F //PID $(cat .verify/pid)` in Git Bash) and nothing else.
3. **The skill.** Fill `SKILL_DIR/templates/verify/SKILL.md` (`{{APP}}` = the repo name,
   `{{APP_KIND}}` = the surfaces) and save it as `.claude/skills/verify-<app>/SKILL.md`. Never
   name it plain `verify`: Claude Code runs a skill with that name before every commit.
4. **The feature map.** Copy `SKILL_DIR/templates/verify/features-README.md` to
   `features/README.md`, and write one `features/<feature>.md` per user-visible feature from
   `SKILL_DIR/templates/verify/feature.md`, with real commands, routes, roles and labels read
   from the code. Add a row per feature to the README table.
5. **Prove it.** Run the skill once end to end: Launch, Doctor, Drive one feature per surface,
   save evidence in `.verify/setup-evidence/`, Cleanup. Then confirm the evidence survived and
   no process from the run is left (`tasklist | grep -i <server process>`). A skill that was
   never run is a draft: fix it and run again until this passes.
6. **First smoke test.** The smoke workflow runs on every push to `main`, the setup PR's merge
   included; with no test marked `smoke` it fails (pytest exits 5 when nothing is selected),
   switches auto-merge off and reverts the setup. So turn the proof run into one smoke test now:
   drive the feature step 5 drove, through the real entry point (the CLI as a subprocess, the
   app over HTTP or Playwright), with an input committed to the repo (`git ls-files` lists it: a
   fixture or a tracked `data/` file), and compare the output with an expected file; for a web
   app also check `/health` or the home page. It needs no secret and no network. pytest:
   `tests/smoke/test_smoke.py` marked `@pytest.mark.smoke`, with the marker registered;
   Playwright: `e2e/smoke.spec.ts` with `@smoke` in the title. Run
   `python scripts/claude/ci_check.py --only smoke` and see `PASS smoke`. The feature's journey
   tests add the rest of the smoke set later (2 to 5 journeys).
7. **Report** the surfaces, the features mapped, the proof run's output and the smoke test.
