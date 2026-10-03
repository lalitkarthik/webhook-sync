---
name: verify-{{APP}}
description: Launch, drive and observe {{APP}} the way a user does, and capture evidence. Used by the feature-loop verifier and auditors. Not run before commits.
---

# Verify {{APP}}

Kind: {{APP_KIND}}. Feature map: `features/README.md`. Evidence goes to `$EVID` (the folder the
orchestrator gave you); never delete it.

## Launch
<!-- exact commands; the port comes from `python scripts/claude/port.py`; a throwaway data dir under .verify/; write the server PID to .verify/pid; how to tell it is ready -->

## Doctor
<!-- one read-only command that proves this instance is up, is the one this run started, and is worth driving -->

## Drive
<!-- the harness per surface: web = `playwright-cli -s="$(basename "$PWD")" …` with role/label locators; CLI = the installed entry point, non-interactive; HTTP = `hurl --variable port=$PORT …` or curl; files = real inputs from fixtures/ or data/ -->

## Evidence
<!-- per AC: one text file (snapshot .yml, stdout .txt, response .json, output .json) and, for UI, a screenshot; how to save each into $EVID -->

## Cleanup
<!-- kill only what Launch started: taskkill //T //F //PID $(cat .verify/pid); close the playwright-cli session; leave $EVID -->
