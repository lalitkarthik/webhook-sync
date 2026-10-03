# Feature map

How to reach and drive every feature of this app as a user. Read by the feature-loop verifier,
the auditors and the test author. One file per feature; keep implementation details out.

Format adapted from pstack (https://github.com/cursor/plugins/tree/main/pstack), MIT License,
Copyright (c) 2026 Lauren Tan.

## Baseline
- Start only through `.claude/skills/verify-{{APP}}/SKILL.md` (Launch). Never drive an instance this run did not start.
- Locators: roles and accessible names first, then data attributes; never coordinates.
- A mutation is proved by a second, read-only view of the stored result.
- Do not report an entry point as verified through a different path.

## Features
| Feature | File | Entry points |
| --- | --- | --- |
