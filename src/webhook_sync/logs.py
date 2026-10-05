"""Logs: one UTF-8 JSON object per line (operations.md, "Logs and health").

Pass known fields with `extra=`, e.g. `log.info("delivery stored", extra={"event_id": ...})`.
Only the fields in LOGGED_FIELDS are written. Never log secrets, signatures, raw bodies, emails
or phone numbers.
"""

import json
import logging
import sys
from typing import Any, TextIO, override

from webhook_sync.clock import to_db_text, utc_now

LOGGED_FIELDS = (
    "event_id",
    "job_id",
    "payment_key",
    "source",
    "result",
    "status_code",
    "reason",
    "header_event_id",
    "fingerprint",
    "stored_fingerprint",
    "attempt",
    "received_at",
    "body_sha256",
    "outcome",
    "error_class",
    "http_status",
    "target",
    "by",
    "alert",
    "suppressed",
    "jobs",
    "rows_updated",
    "rows_appended",
    "duration_ms",
    "list",
    "slice_start",
    "slice_end",
    "listed",
    "stored",
)


class JsonFormatter(logging.Formatter):
    @override
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "time": to_db_text(utc_now()),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in LOGGED_FIELDS:
            value = record.__dict__.get(name)
            if value is not None:
                entry[name] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        # ASCII-only escapes: valid UTF-8 on any console, whatever its encoding (no emoji trap).
        return json.dumps(entry, ensure_ascii=True, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Send every log record, ours and uvicorn's, to stderr as JSON lines. httpx's line per
    request is left out: it holds the whole URL, and Sheets' holds the spreadsheet id."""
    stream: TextIO = sys.stderr
    handler: logging.StreamHandler[TextIO] = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
