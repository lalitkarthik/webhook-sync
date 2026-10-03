-- P3: pauses that survive a restart, the sheet's state and the alert throttle
-- (docs/specs/p3-sheets-sink.md; data-model.md). Every time is UTC text from the one clock.

-- One row per sink and per source. A pause is kept here until `wsync resume` clears it (Q134).
CREATE TABLE sinks (
    name           TEXT PRIMARY KEY,
    kind           TEXT NOT NULL CHECK (kind IN ('sink', 'source')),
    state          TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'paused')),
    reason         TEXT,
    since          TEXT,
    moved_in_a_row INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;

-- One row per spreadsheet: the tab ids the flusher writes by, and the last flush.
CREATE TABLE sheet_state (
    spreadsheet_id       TEXT PRIMARY KEY,
    payments_tab_id      INTEGER,
    daily_summary_tab_id INTEGER,
    sync_status_tab_id   INTEGER,
    deployment_id        TEXT,
    last_flush_at        TEXT,
    rows_written_last    INTEGER NOT NULL DEFAULT 0,
    rows_written_total   INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;

-- One row per alert kind: at most one message an hour, counting the ones held back (Q139).
CREATE TABLE alerts (
    kind             TEXT PRIMARY KEY,
    last_sent_at     TEXT,
    suppressed_count INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;
