# Triage labels

The canonical triage roles map one-to-one onto label strings on this repo's GitHub issues.

| Role | Label |
| --- | --- |
| needs-triage | `needs-triage` |
| needs-info | `needs-info` |
| ready-for-agent | `ready-for-agent` |
| ready-for-human | `ready-for-human` |
| wontfix | `wontfix` |

Labels added by the feature loop:

| Label | Meaning |
| --- | --- |
| `feature` | A feature spec; the parent issue of its tickets |
| `ticket` | One vertical slice of a feature, built by one implementer |
| `in-progress` | An implementer is working on this ticket now |
| `audit` | Found by the audit loop; with `ticket` it is a fix, with `needs-info` it waits on the user, alone it is a minor finding |
| `worker:sonnet-high`, `worker:opus-medium`, `worker:opus-high` | Which implementer builds the ticket (`.claude/skills/feature-loop/workflow.md`, Phase A step 7) |
| `gardener` | Turns a repeated review finding into a rule |
| `human-approved` | The owner approved a PR the risk gate held; only the owner adds it |
