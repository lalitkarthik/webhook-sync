-- P1: events, deliveries, rejection counts, jobs, attempts and the schema version.
-- Columns as docs/design/data-model.md. Every time is UTC text from the one clock.

CREATE TABLE schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE events (
    source            TEXT NOT NULL,
    event_id          TEXT NOT NULL,
    account           TEXT NOT NULL,
    type              TEXT NOT NULL,
    mode              TEXT NOT NULL CHECK (mode IN ('test', 'live')),
    fingerprint       TEXT NOT NULL,
    body_sha256       TEXT,
    payment_key       TEXT,
    origin            TEXT NOT NULL CHECK (origin IN ('delivery', 'sweep', 'backfill', 'replay')),
    outcome           TEXT NOT NULL CHECK (outcome IN ('queued', 'ignored')),
    ignore_reason     TEXT,
    job_id            INTEGER REFERENCES jobs (id),
    payload           TEXT,
    source_created_at TEXT,
    received_at       TEXT NOT NULL,
    PRIMARY KEY (source, event_id)
) WITHOUT ROWID;

-- A signed body replayed under a fresh, unsigned event id is a duplicate (Q86). Partial, so
-- events without a body (the sweeper's synthetic events) never collide.
CREATE UNIQUE INDEX events_body_sha256 ON events (source, body_sha256)
    WHERE body_sha256 IS NOT NULL;

CREATE TABLE deliveries (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    result      TEXT NOT NULL CHECK (result IN ('new', 'duplicate', 'conflict')),
    status_code INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    FOREIGN KEY (source, event_id) REFERENCES events (source, event_id)
);

CREATE INDEX deliveries_event ON deliveries (source, event_id);

CREATE TABLE rejection_counts (
    source TEXT NOT NULL,
    reason TEXT NOT NULL,
    minute TEXT NOT NULL,
    count  INTEGER NOT NULL,
    PRIMARY KEY (source, reason, minute)
) WITHOUT ROWID;

CREATE TABLE jobs (
    id                 INTEGER PRIMARY KEY,
    stage              TEXT NOT NULL,
    sink               TEXT NOT NULL,
    source             TEXT NOT NULL,
    account            TEXT NOT NULL,
    payment_key        TEXT NOT NULL,
    status             TEXT NOT NULL
                       CHECK (status IN ('waiting', 'running', 'done', 'dead', 'resolved')),
    rerun              INTEGER NOT NULL DEFAULT 0,
    attempts           INTEGER NOT NULL DEFAULT 0,
    max_attempts       INTEGER NOT NULL,
    run_after          TEXT NOT NULL,
    lease_until        TEXT,
    claim_token        TEXT,
    claimed_by         TEXT,
    prefetched         TEXT,
    last_error         TEXT,
    last_error_class   TEXT,
    caused_by_event_id TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    resolved_note      TEXT
);

-- The one-live-job key: never a second waiting or running job for a payment (queue.md).
CREATE UNIQUE INDEX jobs_one_live ON jobs (stage, sink, source, account, payment_key)
    WHERE status IN ('waiting', 'running');

CREATE INDEX jobs_due ON jobs (stage, sink, status, run_after);

CREATE TABLE attempts (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES jobs (id),
    number      INTEGER NOT NULL,
    started_at  TEXT,
    finished_at TEXT NOT NULL,
    outcome     TEXT NOT NULL
                CHECK (outcome IN ('ok', 'retry', 'dead', 'pause', 'reaped', 'released')),
    error       TEXT,
    duration_ms INTEGER
);

CREATE INDEX attempts_job ON attempts (job_id);
