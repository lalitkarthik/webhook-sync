# Feature-loop metrics

One line per feature, appended by the orchestrator at its Finish step. The planner reads this
when it labels tickets: a `worker:` label whose first-pass rate falls under 70% stops being used
for that kind of ticket.

| Date | Feature | Tickets | First-pass by label (passed/total) | Escalations | Verifier fails | Held/parked |
| --- | --- | --- | --- | --- | --- | --- |
