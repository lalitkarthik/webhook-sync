# The database is the ledger; the sheet is a mirror

Status: accepted (2026-09-30)

Every event lands in the database first. The database holds the one true row for each payment and enforces "one row per payment" with a unique key. The Google Sheet is written afterwards, in batches, from the database, never straight from an event. If the sheet is damaged, edited or deleted, it is rebuilt from the database.

Writing straight to the sheet is what Zapier and n8n do, and it is exactly where they fail:
- Sheets has no unique constraint and no conditional write, so "check whether the row exists, then append" is a race. n8n's forum shows 100 concurrent events producing 56-63 rows, all marked successful.
- The Sheets API allows 60 writes a minute per user. Writing one event at a time runs into that quota as soon as a burst arrives.

The cost is a second store to run and one more moving part between an event and the row the buyer sees: rows appear a few seconds late. Buyers who want only a sheet still get one; the database is a single SQLite file in version 1.
