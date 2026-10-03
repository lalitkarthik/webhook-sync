"""What `wsync status` and `GET /admin/status` show (Q133): each sink and source with its state,
reason and since; jobs waiting and dead by stage; and the last flush.

Read from the database, so the server and the CLI without a server give the same answer.
"""

from pathlib import Path
from typing import Any, TypedDict

from webhook_sync.db.reader import Reader

STAGES = ("ledger", "mirror")
"""The stages always listed, even with no jobs."""


class SinkStatus(TypedDict):
    name: str
    kind: str
    state: str
    reason: str | None
    since: str | None


class JobCounts(TypedDict):
    waiting: int
    dead: int


class LastFlush(TypedDict):
    at: str | None
    rows_written: int


class Status(TypedDict):
    sinks: list[SinkStatus]
    jobs: dict[str, JobCounts]
    last_flush: LastFlush


async def read_status(data_dir: Path) -> Status:
    async with Reader.open(data_dir) as reader:
        sinks = await reader.fetch_all(
            "SELECT name, kind, state, reason, since FROM sinks ORDER BY kind DESC, name"
        )
        counts = await reader.fetch_all(
            "SELECT stage, status, count(*) AS n FROM jobs"
            " WHERE status IN ('waiting', 'dead') GROUP BY stage, status"
        )
        flushes = await reader.fetch_all(
            "SELECT last_flush_at, rows_written_last FROM sheet_state"
            " ORDER BY last_flush_at DESC LIMIT 1"
        )
    jobs: dict[str, JobCounts] = {stage: {"waiting": 0, "dead": 0} for stage in STAGES}
    for row in counts:
        jobs.setdefault(row["stage"], {"waiting": 0, "dead": 0})[row["status"]] = row["n"]
    flush: dict[str, Any] = flushes[0] if flushes else {}
    return {
        "sinks": [
            {
                "name": row["name"],
                "kind": row["kind"],
                "state": row["state"],
                "reason": row["reason"],
                "since": row["since"],
            }
            for row in sinks
        ],
        "jobs": jobs,
        "last_flush": {
            "at": flush.get("last_flush_at"),
            "rows_written": flush.get("rows_written_last") or 0,
        },
    }


def format_status(status: Status) -> str:
    """The status as `wsync status` prints it: one line per sink and source, then the jobs and
    the last flush."""
    lines: list[str] = []
    for sink in status["sinks"]:
        line = f"{sink['name']} ({sink['kind']}): {sink['state']}"
        if sink["state"] == "paused":
            line += f" since {sink['since']}: {sink['reason']}"
        lines.append(line)
    lines.append(
        "jobs: "
        + "; ".join(
            f"{stage} {counts['waiting']} waiting, {counts['dead']} dead"
            for stage, counts in status["jobs"].items()
        )
    )
    flush = status["last_flush"]
    lines.append(
        f"last flush: {flush['at']}, {flush['rows_written']} rows written"
        if flush["at"]
        else "last flush: never"
    )
    return "\n".join(lines)
