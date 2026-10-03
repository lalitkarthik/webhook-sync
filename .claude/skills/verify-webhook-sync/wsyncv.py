"""Launch, check, drive and clean up one isolated `wsync serve` for verification.

Run with the venv's own interpreter from the repo root (not `uv run`: on Windows uv's job
object would take the detached server down when uv exits):

    PY=.venv/Scripts/python.exe; [ -x "$PY" ] || PY=.venv/bin/python
    $PY .claude/skills/verify-webhook-sync/wsyncv.py launch
    $PY .claude/skills/verify-webhook-sync/wsyncv.py doctor
    $PY .claude/skills/verify-webhook-sync/wsyncv.py deliver Tj9sMcl7hE9PaT
    $PY .claude/skills/verify-webhook-sync/wsyncv.py sql "SELECT * FROM payments"
    $PY .claude/skills/verify-webhook-sync/wsyncv.py cleanup

`launch` starts one detached process tree whose top PID goes to `.verify/pid`: a supervisor
serving the fake upstreams (`tests/fakes/upstreams.py`: Razorpay's API replaying the recordings,
and a Google token endpoint that refuses the key) and its child, the installed `wsync serve`.
Fixture secrets only (`tests/support.py`); no call leaves 127.0.0.1. Everything it writes is
under `.verify/run/`; `cleanup` kills that PID tree only and keeps any evidence folder.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VERIFY = ROOT / ".verify"
RUN = VERIFY / "run"
PID_FILE = VERIFY / "pid"
STATE = RUN / "state.json"
CONFIG = RUN / "webhook-sync.toml"
DATABASE = RUN / "data" / "webhook-sync.db"
SERVE_LOG = RUN / "serve.log"
UPSTREAMS_LOG = RUN / "upstreams.log"
WINDOWS = sys.platform == "win32"

sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "tests" / "fakes")]


def _wsync() -> str:
    found = shutil.which("wsync", path=str(Path(sys.executable).parent))
    if found is None:
        sys.exit("wsyncv: no wsync next to this python; run `uv sync`, then use .venv's python")
    return found


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _alive(pid: int) -> bool:
    if WINDOWS:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _get(url: str) -> tuple[int, str]:
    import httpx

    try:
        answer = httpx.get(url, timeout=2)
    except httpx.HTTPError as error:
        return 0, type(error).__name__
    return answer.status_code, answer.text


def _state() -> dict[str, int | str]:
    if not STATE.is_file():
        sys.exit("wsyncv: nothing launched (.verify/run/state.json missing); run launch")
    return json.loads(STATE.read_text(encoding="utf-8"))


def launch(args: argparse.Namespace) -> int:
    import processes  # tests/processes.py
    import support  # tests/support.py

    if PID_FILE.is_file() and _alive(int(PID_FILE.read_text())):
        print(f"wsyncv: already running as PID {PID_FILE.read_text().strip()}; run cleanup first")
        return 1
    port = int(
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "claude" / "port.py")],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
        ).stdout.strip()
    )
    admin_port, upstreams_port = _free_port(), _free_port()
    shutil.rmtree(RUN, ignore_errors=True)
    RUN.mkdir(parents=True)
    CONFIG.write_text(
        f'[service]\npublic_listen = "127.0.0.1:{port}"\ndata_dir = "data"\n'
        f'admin_listen = "127.0.0.1:{admin_port}"\ntimezone = "Asia/Kolkata"\n'
        f'retry_profile = "demo"\n\n'
        f'[sources.razorpay]\nmode = "test"\naccount = "{support.RECORDED_ACCOUNT}"\n'
        f'api_url = "http://127.0.0.1:{upstreams_port}"\n\n'
        f'[sinks.sheets]\nspreadsheet_id = "{support.TEST_SPREADSHEET_ID}"\n\n'
        f'[demo]\nspreadsheet_ids = ["{support.TEST_SPREADSHEET_ID}"]\n',
        encoding="utf-8",
    )
    state: dict[str, int | str] = {
        "port": port,
        "admin_port": admin_port,
        "upstreams_port": upstreams_port,
        "config": CONFIG.relative_to(ROOT).as_posix(),
        "demo_fail_first": args.demo_fail_first or 0,
    }
    STATE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    (RUN / "admin_port").write_text(str(admin_port), encoding="utf-8")
    (RUN / "upstreams_port").write_text(str(upstreams_port), encoding="utf-8")
    environ = processes.fixture_environment()
    environ["GOOGLE_SERVICE_ACCOUNT_B64"] = support.fixture_service_account_b64(
        token_uri=f"http://127.0.0.1:{upstreams_port}/token"
    )
    flags: dict[str, object] = (
        {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        if WINDOWS
        else {"start_new_session": True}
    )
    with UPSTREAMS_LOG.open("wb") as log:
        supervisor = subprocess.Popen(
            [sys.executable, __file__, "_supervise"],
            cwd=ROOT,
            env=environ,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            **flags,  # pyright: ignore[reportArgumentType]
        )
    PID_FILE.write_text(str(supervisor.pid), encoding="utf-8")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if supervisor.poll() is not None:
            print(f"wsyncv: the server exited ({supervisor.returncode}); see {SERVE_LOG}")
            print(SERVE_LOG.read_text(encoding="utf-8", errors="replace")[-2000:])
            return 1
        health, _ = _get(f"http://127.0.0.1:{port}/health")
        admin, _ = _get(f"http://127.0.0.1:{admin_port}/admin/status")
        if health == 200 and admin == 200:
            print(f"ready: PID {supervisor.pid} (.verify/pid)")
            print(f"public  http://127.0.0.1:{port}  (POST /webhooks/razorpay, GET /health)")
            print(f"admin   http://127.0.0.1:{admin_port}  (GET /admin/status, POST /admin/resume/<name>)")
            print(f"fakes   http://127.0.0.1:{upstreams_port}  (Razorpay API, Google token)")
            print(f"config  {state['config']}")
            print("ports   .verify/port, .verify/run/admin_port, .verify/run/upstreams_port")
            return 0
        time.sleep(0.2)
    print("wsyncv: not healthy within 30 s; run cleanup and read .verify/run/serve.log")
    return 1


def supervise(_: argparse.Namespace) -> int:
    """The detached top of the tree: the fake upstreams in a thread, `wsync serve` as a child."""
    from upstreams import start_upstreams

    state = _state()
    upstreams = start_upstreams(int(state["upstreams_port"]))
    with SERVE_LOG.open("wb") as log:
        server = subprocess.Popen(
            [_wsync(), "serve", "--config", str(CONFIG)]
            + (["--demo-fail-first", str(n)] if (n := int(state["demo_fail_first"])) else []),
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
        )
        code = server.wait()
    upstreams.shutdown()
    return code


def doctor(_: argparse.Namespace) -> int:
    state = _state()
    pid = int(PID_FILE.read_text()) if PID_FILE.is_file() else 0
    checks = {
        "pid": (pid > 0 and _alive(pid), f"PID {pid} from .verify/pid"),
    }
    health = _get(f"http://127.0.0.1:{state['port']}/health")
    checks["health"] = (health == (200, '{"status":"ok"}'), f"/health {health[0]} {health[1]}")
    status = subprocess.run(
        [_wsync(), "status", "--config", str(CONFIG)],
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
    )
    first = (status.stdout.splitlines() or [status.stderr.strip()])[0]
    checks["owner"] = (
        first == "server: through the running server",
        f"wsync status on .verify/run: {first}",
    )
    for name, (ok, detail) in checks.items():
        print(f"{'OK  ' if ok else 'FAIL'} {name}: {detail}")
    return 0 if all(ok for ok, _ in checks.values()) else 1


def deliver(args: argparse.Namespace) -> int:
    import httpx
    import support

    raw = support.recorded_delivery(args.event_id, args.attempt)
    body, headers = support.sign_razorpay(raw, event_id=args.header_event_id or args.event_id)
    if args.bad_signature:
        headers["x-razorpay-signature"] = "0" * 64
    state = _state()
    answer = httpx.post(
        f"http://127.0.0.1:{state['port']}/webhooks/razorpay", content=body, headers=headers
    )
    event = json.loads(body)
    print(
        json.dumps(
            {
                "request": {
                    "url": f"POST /webhooks/razorpay on {state['port']}",
                    "event": event.get("event"),
                    "x-razorpay-event-id": headers["x-razorpay-event-id"],
                    "body_bytes": len(body),
                },
                "status": answer.status_code,
                "body": answer.json(),
            },
            indent=2,
        )
    )
    return 0


def sql(args: argparse.Namespace) -> int:
    """Print the query's rows as JSON; with --wait S, poll until it returns a row (or S pass)."""
    deadline = time.monotonic() + args.wait
    while True:
        with closing(sqlite3.connect(f"{DATABASE.as_uri()}?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute(args.query).fetchall()]
        if rows or time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    print(json.dumps(rows, indent=2, default=str))
    return 0 if rows or not args.wait else 1


def cleanup(_: argparse.Namespace) -> int:
    if PID_FILE.is_file():
        pid = int(PID_FILE.read_text())
        if _alive(pid):
            if WINDOWS:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], check=False)
            else:
                os.killpg(pid, 15)
                for _ in range(100):
                    if not _alive(pid):
                        break
                    time.sleep(0.1)
        print(f"PID tree {pid}: {'still running' if _alive(pid) else 'gone'}")
        PID_FILE.unlink()
    for _ in range(50):  # Windows frees the database files a moment after the kill
        shutil.rmtree(RUN, ignore_errors=True)
        if not RUN.exists():
            break
        time.sleep(0.1)
    print(f".verify/run removed: {not RUN.exists()}; evidence folders kept")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="wsyncv", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    la = sub.add_parser("launch", help="start a fresh server under .verify/run")
    la.add_argument("--demo-fail-first", type=int, metavar="N", help="pass to wsync serve")
    la.set_defaults(run=launch)
    sub.add_parser("_supervise").set_defaults(run=supervise)
    sub.add_parser("doctor").set_defaults(run=doctor)
    d = sub.add_parser("deliver", help="POST a recorded delivery, re-signed with the fixture secret")
    d.add_argument("event_id", help="a recording under tests/recordings/razorpay/, e.g. Tj9sMcl7hE9PaT")
    d.add_argument("--attempt", type=int, default=1)
    d.add_argument("--header-event-id", help="send this x-razorpay-event-id instead")
    d.add_argument("--bad-signature", action="store_true")
    d.set_defaults(run=deliver)
    q = sub.add_parser("sql", help="a read-only query on .verify/run/data/webhook-sync.db")
    q.add_argument("query")
    q.add_argument("--wait", type=float, default=0, metavar="S", help="poll up to S s for a row")
    q.set_defaults(run=sql)
    sub.add_parser("cleanup").set_defaults(run=cleanup)
    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
