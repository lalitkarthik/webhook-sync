# The service owns its tabs; buyers work in their own

Status: accepted (2026-09-30), after the design audit (Q74).

The Payments, Daily summary and Sync status tabs belong to the service. It writes every cell in them, never rewrites a key it has written, and repairs them in place. Buyers read them, sort and filter them through Sheets' **filter views** (which change only the viewer's own view), and put their own columns, notes and formulas in tabs of their own, looking rows up by payment id.

The first design let buyers add columns next to ours, edit our cells and sort the live tab, and tried to cope: a header check, a post-write check, warning-only protection, and a rebuild that swapped in a new tab. The audit found three bugs that all came from sharing one tab with a person:
- a sort between our read and our write sends a payment's values onto another payment's row, and the check could not see it (F05);
- new rows landed either a thousand rows down or on top of a buyer's total row (F07);
- the rebuild detached the buyer's own columns from their rows, and a crash mid-swap left the service writing to the old tab while verify agreed with it (F06).

One rule removes all three. The cost is a small change of habit for the buyer, explained in one line of the header note: "Managed by webhook-sync. Sort and filter with Data › Create filter view; put your own work in another tab." Sorting the tab directly still works; the post-write check notices a moved row and repairs it, only more slowly.
