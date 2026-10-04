"""What `wsync status` and `GET /admin/status` show (Q133): each sink and source with its state,
reason and since; jobs waiting and dead by stage; the last flush; the last sweep of each list
(its checkpoint, when it last moved and what that pass stored); and the last verify line.

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


class LastSweep(TypedDict):
    list: str
    checkpoint_at: str
    last_sweep_at: str | None
    stored: int


class Status(TypedDict):
    sinks: list[SinkStatus]
    jobs: dict[str, JobCounts]
    last_flush: LastFlush
    sweeps: list[LastSweep]
    last_verify: str | None
    """The latest verify run's line, as Sync status shows it."""


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
        sweeps = await reader.fetch_all(
            "SELECT list, checkpoint_at, last_sweep_at, last_sweep_stored FROM checkpoints"
            " ORDER BY list"
        )
        verified = await reader.fetch_all(
            "SELECT status_line FROM verify_runs WHERE status_line IS NOT NULL"
            " ORDER BY id DESC LIMIT 1"
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
        "sweeps": [
            {
                "list": row["list"],
                "checkpoint_at": row["checkpoint_at"],
                "last_sweep_at": row["last_sweep_at"],
                "stored": row["last_sweep_stored"],
            }
            for row in sweeps
        ],
        "last_verify": verified[0]["status_line"] if verified else None,
    }


def format_status(status: Status) -> str:
    """The status as `wsync status` prints it: one line per sink and source, then the jobs, the
    last flush, the last sweep of each list and the last verify."""
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
    for sweep in status["sweeps"]:
        lines.append(
            f"last sweep of {sweep['list']}: {sweep['last_sweep_at']}, {sweep['stored']} stored;"
            f" checkpoint {sweep['checkpoint_at']}"
            if sweep["last_sweep_at"]
            else f"last sweep of {sweep['list']}: never; checkpoint {sweep['checkpoint_at']}"
        )
    if not status["sweeps"]:
        lines.append("last sweep: never")
    lines.append(f"last verify: {status['last_verify'] or 'never'}")
    return "\n".join(lines)
