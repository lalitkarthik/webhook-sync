"""The one clock (A14): the current UTC time comes only from here and is passed into SQL.

SQLite's own `now` is never used, so tests can move time with time-machine. Times are stored as
text in one fixed-width form, so they sort and compare as strings.
"""

from datetime import UTC, datetime


def utc_now() -> datetime:
    """The current time, in UTC."""
    return datetime.now(UTC)


def to_db_text(moment: datetime) -> str:
    """The stored form of a time: UTC ISO 8601 with microseconds (2026-10-02T09:00:00.000000Z)."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
