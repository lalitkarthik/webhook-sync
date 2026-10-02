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
