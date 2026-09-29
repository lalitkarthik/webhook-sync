# Permissive licences only

Status: accepted (2026-09-30)

Code is copied or ported only from MIT, BSD or Apache-2.0 projects, and every such file keeps its notice. Code under AGPL, GPL, the Elastic License or no licence at all is read for ideas and written again from scratch.

This repository is a starter that gets forked into paid client work, and the client owns the code on final payment. A copyleft file inside it could oblige the client to publish their code, and the Elastic License forbids offering the software as a hosted service. In the studied repos this rules out Convoy (Elastic License 2.0) as a source of code: its per-source dedupe-key idea is reimplemented, not copied.

Ports from Apache-2.0 projects (driftless's queue and sweeper, sync-engine's Sheets writer) carry a header naming the original file and saying what was changed, and `THIRD_PARTY_NOTICES.md` holds every licence text.
