-- P4: the sweeper's checkpoints and verify's runs and findings
-- (docs/specs/p4-sweeper-verify.md, "Schema"; data-model.md). Every time is UTC text; a
-- checkpoint is on the source's clock, every other time on ours.

-- One row per source, account and list. Every item created before `checkpoint_at` has been
-- listed and stored at least once (sweeper.md). The hourly re-list never reads items
-- created before `relist_floor_at`, the list's first checkpoint: a first start reads
-- only its lookback, and older history comes by backfill (Q157). `first_start_at` is
-- Razorpay's now at the first start: items created before it are the lookback's
-- history, which no delivery was ever due for (no silent-webhook alert).
CREATE TABLE checkpoints (
    source            TEXT NOT NULL,
    account           TEXT NOT NULL,
    list              TEXT NOT NULL CHECK (list IN ('payments', 'refunds')),
    checkpoint_at     TEXT NOT NULL,
    last_sweep_at     TEXT,
    last_sweep_stored INTEGER NOT NULL DEFAULT 0,
    last_relist_at    TEXT,
    relist_floor_at   TEXT NOT NULL,
    first_start_at    TEXT NOT NULL,
    PRIMARY KEY (source, account, list)
) WITHOUT ROWID;

-- One row per verify run: quick (the sheet against the ledger) or full (Razorpay too), its
-- counts and the line it puts on Sync status. `local_date` is the full run's date in
-- `service.timezone`, so the nightly run is once per date across restarts (A18). A run asked
-- for by hand (`by_hand` 1) never counts as a night's run.
CREATE TABLE verify_runs (
    id               INTEGER PRIMARY KEY,
    kind             TEXT NOT NULL CHECK (kind IN ('quick', 'full')),
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    window_days      INTEGER NOT NULL,
    local_date       TEXT NOT NULL,
    payments_checked INTEGER NOT NULL DEFAULT 0,
    findings_count   INTEGER NOT NULL DEFAULT 0,
    repaired_count   INTEGER NOT NULL DEFAULT 0,
    ignored_events   INTEGER NOT NULL DEFAULT 0,
    outcome          TEXT,
    status_line      TEXT,
    by_hand          INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX verify_runs_by_kind_and_date ON verify_runs (kind, local_date);

-- One row per difference a run found, with the job that repaired it when one was enqueued.
CREATE TABLE verify_findings (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER NOT NULL REFERENCES verify_runs (id),
    kind          TEXT NOT NULL,
    source        TEXT NOT NULL,
    account       TEXT NOT NULL,
    payment_id    TEXT,
    detail        TEXT,
    repair_job_id INTEGER
);

CREATE INDEX verify_findings_by_run ON verify_findings (run_id);
