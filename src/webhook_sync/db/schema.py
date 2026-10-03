"""The tables, described for SQLAlchemy Core to build statements against.

The migrations in `migrations/` create the tables; these descriptions only name their columns.
Times are UTC text from the one clock (`webhook_sync.clock`). Add a table here when a statement
needs it.
"""

import sqlalchemy as sa

metadata = sa.MetaData()

events = sa.Table(
    "events",
    metadata,
    sa.Column("source", sa.Text, primary_key=True),
    sa.Column("event_id", sa.Text, primary_key=True),
    sa.Column("account", sa.Text, nullable=False),
    sa.Column("type", sa.Text, nullable=False),
    sa.Column("mode", sa.Text, nullable=False),
    sa.Column("fingerprint", sa.Text, nullable=False),
    sa.Column("body_sha256", sa.Text),
    sa.Column("payment_key", sa.Text),
    sa.Column("origin", sa.Text, nullable=False),
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column("ignore_reason", sa.Text),
    sa.Column("job_id", sa.Integer),
    sa.Column("payload", sa.Text),
    sa.Column("source_created_at", sa.Text),
    sa.Column("received_at", sa.Text, nullable=False),
)

deliveries = sa.Table(
    "deliveries",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("event_id", sa.Text, nullable=False),
    sa.Column("result", sa.Text, nullable=False),
    sa.Column("status_code", sa.Integer, nullable=False),
    sa.Column("received_at", sa.Text, nullable=False),
    sa.Column("duration_ms", sa.Integer, nullable=False),
)

jobs = sa.Table(
    "jobs",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("stage", sa.Text, nullable=False),
    sa.Column("sink", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("account", sa.Text, nullable=False),
    sa.Column("payment_key", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("rerun", sa.Integer, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False),
    sa.Column("max_attempts", sa.Integer, nullable=False),
    sa.Column("run_after", sa.Text, nullable=False),
    sa.Column("lease_until", sa.Text),
    sa.Column("claim_token", sa.Text),
    sa.Column("claimed_by", sa.Text),
    sa.Column("prefetched", sa.Text),
    sa.Column("last_error", sa.Text),
    sa.Column("last_error_class", sa.Text),
    sa.Column("caused_by_event_id", sa.Text),
    sa.Column("created_at", sa.Text, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
    sa.Column("resolved_note", sa.Text),
)

attempts = sa.Table(
    "attempts",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("job_id", sa.Integer, nullable=False),
    sa.Column("number", sa.Integer, nullable=False),
    sa.Column("started_at", sa.Text),
    sa.Column("finished_at", sa.Text, nullable=False),
    sa.Column("outcome", sa.Text, nullable=False),
    sa.Column("error", sa.Text),
    sa.Column("duration_ms", sa.Integer),
)


rejection_counts = sa.Table(
    "rejection_counts",
    metadata,
    sa.Column("source", sa.Text, primary_key=True),
    sa.Column("reason", sa.Text, primary_key=True),
    sa.Column("minute", sa.Text, primary_key=True),
    sa.Column("count", sa.Integer, nullable=False),
)


payments = sa.Table(
    "payments",
    metadata,
    sa.Column("source", sa.Text, primary_key=True),
    sa.Column("account", sa.Text, primary_key=True),
    sa.Column("payment_id", sa.Text, primary_key=True),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("source_status", sa.Text, nullable=False),
    sa.Column("amount_minor", sa.Integer, nullable=False),
    sa.Column("authorized_minor", sa.Integer),
    sa.Column("currency", sa.Text, nullable=False),
    sa.Column("refunded_minor", sa.Integer, nullable=False),
    sa.Column("fee_minor", sa.Integer),
    sa.Column("fee_tax_minor", sa.Integer),
    sa.Column("net_minor", sa.Integer),
    sa.Column("settlement_minor", sa.Integer),
    sa.Column("settlement_currency", sa.Text),
    sa.Column("method", sa.Text),
    sa.Column("metadata", sa.Text),
    sa.Column("dispute_status", sa.Text),
    sa.Column("customer_email", sa.Text),
    sa.Column("customer_name", sa.Text),
    sa.Column("description", sa.Text),
    sa.Column("source_created_at", sa.Text),
    sa.Column("not_found_at", sa.Text),
    sa.Column("unknown_columns", sa.Text),
    sa.Column("via", sa.Text),
    sa.Column("last_event_id", sa.Text),
    sa.Column("row_hash", sa.Text, nullable=False),
    sa.Column("row_version", sa.Integer, nullable=False),
    sa.Column("updated_at", sa.Text, nullable=False),
)


sinks = sa.Table(
    "sinks",
    metadata,
    sa.Column("name", sa.Text, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("reason", sa.Text),
    sa.Column("since", sa.Text),
    sa.Column("moved_in_a_row", sa.Integer, nullable=False),
)


sheet_state = sa.Table(
    "sheet_state",
    metadata,
    sa.Column("spreadsheet_id", sa.Text, primary_key=True),
    sa.Column("payments_tab_id", sa.Integer),
    sa.Column("daily_summary_tab_id", sa.Integer),
    sa.Column("sync_status_tab_id", sa.Integer),
    sa.Column("deployment_id", sa.Text),
    sa.Column("last_flush_at", sa.Text),
    sa.Column("rows_written_last", sa.Integer, nullable=False),
    sa.Column("rows_written_total", sa.Integer, nullable=False),
)


alerts = sa.Table(
    "alerts",
    metadata,
    sa.Column("kind", sa.Text, primary_key=True),
    sa.Column("last_sent_at", sa.Text),
    sa.Column("suppressed_count", sa.Integer, nullable=False),
)


checkpoints = sa.Table(
    "checkpoints",
    metadata,
    sa.Column("source", sa.Text, primary_key=True),
    sa.Column("account", sa.Text, primary_key=True),
    sa.Column("list", sa.Text, primary_key=True),
    sa.Column("checkpoint_at", sa.Text, nullable=False),
    sa.Column("last_sweep_at", sa.Text),
    sa.Column("last_sweep_stored", sa.Integer, nullable=False),
    sa.Column("last_relist_at", sa.Text),
    sa.Column("relist_floor_at", sa.Text, nullable=False),
)


verify_runs = sa.Table(
    "verify_runs",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("started_at", sa.Text, nullable=False),
    sa.Column("finished_at", sa.Text),
    sa.Column("window_days", sa.Integer, nullable=False),
    sa.Column("local_date", sa.Text, nullable=False),
    sa.Column("payments_checked", sa.Integer, nullable=False),
    sa.Column("findings_count", sa.Integer, nullable=False),
    sa.Column("repaired_count", sa.Integer, nullable=False),
    sa.Column("ignored_events", sa.Integer, nullable=False),
    sa.Column("outcome", sa.Text),
    sa.Column("status_line", sa.Text),
)


verify_findings = sa.Table(
    "verify_findings",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("run_id", sa.Integer, nullable=False),
    sa.Column("kind", sa.Text, nullable=False),
    sa.Column("source", sa.Text, nullable=False),
    sa.Column("account", sa.Text, nullable=False),
    sa.Column("payment_id", sa.Text),
    sa.Column("detail", sa.Text),
    sa.Column("repair_job_id", sa.Integer),
)
