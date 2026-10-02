-- P2: the ledger, one business row per payment (docs/design/data-model.md, "payments").
-- Amounts are integers in minor units; metadata and unknown_columns are JSON text; every time is
-- UTC text from the one clock. Null fee, net and settlement mean pending, not zero.

CREATE TABLE payments (
    source              TEXT NOT NULL,
    account             TEXT NOT NULL,
    payment_id          TEXT NOT NULL,
    status              TEXT NOT NULL
                        CHECK (status IN ('processing', 'authorized', 'failed', 'succeeded',
                                          'partially_refunded', 'refunded', 'canceled')),
    source_status       TEXT NOT NULL,
    amount_minor        INTEGER NOT NULL,
    authorized_minor    INTEGER,
    currency            TEXT NOT NULL,
    refunded_minor      INTEGER NOT NULL DEFAULT 0,
    fee_minor           INTEGER,
    fee_tax_minor       INTEGER,
    net_minor           INTEGER,
    settlement_minor    INTEGER,
    settlement_currency TEXT,
    method              TEXT,
    metadata            TEXT,
    dispute_status      TEXT
                        CHECK (dispute_status IN ('inquiry', 'needs_response', 'under_review',
                                                  'won', 'lost', 'prevented', 'closed')),
    customer_email      TEXT,
    customer_name       TEXT,
    description         TEXT,
    source_created_at   TEXT,
    not_found_at        TEXT,
    unknown_columns     TEXT,
    via                 TEXT CHECK (via IN ('delivery', 'sweep')),
    last_event_id       TEXT,
    row_hash            TEXT NOT NULL,
    row_version         INTEGER NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (source, account, payment_id)
) WITHOUT ROWID;
