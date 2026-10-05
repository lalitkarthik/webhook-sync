"""`wsync demo reset`, `outage`, `duplicates` and `verify` as real subprocesses (seam 3; Q105,
Q120, Q181).

Razorpay and Google are FakeRazorpay and FakeSheets over HTTP on 127.0.0.1, as in the command
tests of P4 and the crash tests of P5, and the payer is a fake one (`fakes.payer`) the command
loads by `WSYNC_DEMO_PAYER`: it pays an order through FakeRazorpay's checkout call, where the
real payer drives a browser. What comes out is what the owner sees: the printed lines, the exit
code, the ledger, the sheet and the config file.
"""

import asyncio
import contextlib
import copy
import json
import os
import queue
import re
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from typing import ClassVar

import httpx
import pytest
from conftest import runs_live
from crash.harness import CrashRun, served_run
from fakes.payer import FAILING_PAYER, FAKE_PAYER
from fakes.sheets import FakeSheets, Tab, edit_cell, rename_tab
from invariants import buyers_tabs
from processes import fixture_environment, free_port, stop, write_serve_config, wsync_path
from support import (
    TEST_ACCOUNT,
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_SPREADSHEET_ID,
    TEST_WEBHOOK_SECRET,
    ReadRows,
    fixture_secrets,
)

from webhook_sync.cli import demo as demo_scenes
from webhook_sync.cli.admin import CommandFailed
from webhook_sync.cli.demo import stop_receiver, with_demo_retry_profile
from webhook_sync.clock import to_db_text
from webhook_sync.config import load_settings
from webhook_sync.db.lock import hold_lock
from webhook_sync.demo.payer import CHECKOUT_JS, PAYER_VARIABLE
from webhook_sync.sinks.encoding import payment_columns
from webhook_sync.sinks.sheets import (
    DAILY_SUMMARY,
    DAILY_SUMMARY_HEADER,
    PAYMENTS,
    SYNC_STATUS,
    SYNC_STATUS_HEADER,
)
from webhook_sync.sweeper.sweep import OVERLAP, SETTLE

SCENES = ("reset", "outage", "duplicates", "verify")
LIVE_KEY_ID = "rzp_live_fixtureKeyId01"
SECRET_VALUES = (
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_WEBHOOK_SECRET,
    LIVE_KEY_ID,
    TEST_SPREADSHEET_ID,
)
"""What no scene may print: the secrets, and the spreadsheet's id (sheets-sink.md)."""
ON_THE_DEMO_LIST = f'\n[demo]\nspreadsheet_ids = ["{TEST_SPREADSHEET_ID}"]\n'
QUIET_VERIFY = '\n[verify]\nnightly_at = "23:59"\n'
"""The nightly verify never due in a test day's hours."""


def demo(config_path: Path, scene: str, environ: dict[str, str]) -> tuple[int, str]:
    result = subprocess.run(
        [wsync_path(), "demo", scene, "--config", str(config_path)],
        env=environ,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = result.stdout + result.stderr
    assert [value for value in SECRET_VALUES if value in output] == []
    return result.returncode, output


@pytest.mark.parametrize("scene", SCENES)
def test_every_scene_refuses_a_key_that_is_not_a_test_key(tmp_path: Path, scene: str) -> None:
    config_path = write_serve_config(tmp_path, free_port(), extra=ON_THE_DEMO_LIST)
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace('mode = "test"', 'mode = "live"'),
        encoding="utf-8",
    )
    environ = fixture_environment() | {"RAZORPAY_KEY_ID": LIVE_KEY_ID}

    code, output = demo(config_path, scene, environ)

    assert code == 2
    assert f"wsync demo {scene}: wsync demo needs a test-mode key" in output
    assert "RAZORPAY_KEY_ID must start rzp_test_" in output
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("scene", SCENES)
@pytest.mark.parametrize("listed", ["", '\n[demo]\nspreadsheet_ids = ["another-sheet-0002"]\n'])
def test_every_scene_refuses_a_sheet_not_on_the_demo_list(
    tmp_path: Path, scene: str, listed: str
) -> None:
    config_path = write_serve_config(tmp_path, free_port(), extra=listed)

    code, output = demo(config_path, scene, fixture_environment())

    assert code == 2
    assert f"wsync demo {scene}: wsync demo needs sinks.sheets.spreadsheet_id listed in" in output
    assert "demo.spreadsheet_ids" in output
    assert not (tmp_path / "data").exists()


# -- demo reset ------------------------------------------------------------------------------

HOUR = 3600
EMPTIED = ("events", "deliveries", "jobs", "attempts", "payments", "checkpoints", "sheet_state")


@pytest.fixture
def run(data_dir: Path) -> Iterator[CrashRun]:
    """A config on the demo list, with FakeRazorpay and FakeSheets over HTTP; the first sweep
    reads one day back, and the buyer has tabs of their own beside ours."""
    with served_run(data_dir, lookback_days=1, extra=ON_THE_DEMO_LIST + QUIET_VERIFY) as run:
        run.sheets.add_buyers_own()
        run.buyers_tabs = buyers_tabs(run.sheets)
        yield run


async def swept_in(run: CrashRun, *payment_ids: str) -> None:
    """Razorpay made the payments two hours ago; a server sweeps them in and settles."""
    created = int(run.razorpay.now.timestamp()) - 2 * HOUR
    for payment_id in payment_ids:
        run.razorpay.make(payment_id, created)
    async with run.server() as server:
        assert (await server.run_ended(await server.sweep()))["state"] == "finished"
        await run.settle()


async def wsync_demo(run: CrashRun, scene: str) -> tuple[int, str]:
    return await asyncio.to_thread(demo, run.config_path, scene, run.environ)


async def test_reset_empties_the_database_and_our_tabs_and_sets_the_demo_retry_profile(
    run: CrashRun, read_rows: ReadRows
) -> None:
    await swept_in(run, "pay_DemoReset0001", "pay_DemoReset0002")
    assert len(run.sheets.tab(PAYMENTS).values()) == 3  # the header and the two rows

    code, output = await wsync_demo(run, "reset")

    assert code == 0, output
    assert "database emptied" in output
    assert "Payments, Daily summary and Sync status emptied" in output
    assert 'retry_profile = "demo"' in output
    for table in EMPTIED:
        assert await read_rows(f"SELECT * FROM {table}") == [], table
    assert {row["name"] for row in await read_rows("SELECT name FROM sinks")} == {
        "razorpay",
        "sheets",
    }
    assert run.sheets.tab(PAYMENTS).values() == [[c.header for c in payment_columns(run.config)]]
    assert run.sheets.tab(DAILY_SUMMARY).values() == [list(DAILY_SUMMARY_HEADER)]
    assert run.sheets.tab(SYNC_STATUS).values() == [list(SYNC_STATUS_HEADER)]
    now = {tab.title: tab.cells for tab in run.sheets.tabs}
    assert [(t.title, t.cells) for t in run.buyers_tabs] == [
        (t.title, now.get(t.title)) for t in run.buyers_tabs
    ]
    assert load_settings(run.config_path, run.environ).service.retry_profile == "demo"


async def test_after_a_reset_a_server_writes_the_sheet_again_from_an_empty_ledger(
    run: CrashRun, read_rows: ReadRows
) -> None:
    await swept_in(run, "pay_DemoReset0001")
    assert (await wsync_demo(run, "reset"))[0] == 0
    run.razorpay.payments.clear()

    await swept_in(run, "pay_DemoReset0003")

    _, *rows = run.sheets.tab(PAYMENTS).values()
    assert [row[0] for row in rows] == ["pay_DemoReset0003"]
    assert [row["payment_id"] for row in await read_rows("SELECT payment_id FROM payments")] == [
        "pay_DemoReset0003"
    ]


async def test_reset_refuses_while_a_server_holds_the_database(
    run: CrashRun, read_rows: ReadRows
) -> None:
    await swept_in(run, "pay_DemoReset0001")
    before = run.config_path.read_text(encoding="utf-8")

    async with run.server():
        code, output = await wsync_demo(run, "reset")
        stored = await read_rows("SELECT payment_id FROM payments")

    assert code == 2
    assert "a server is running: stop wsync serve first" in output
    assert [row["payment_id"] for row in stored] == ["pay_DemoReset0001"]
    assert len(run.sheets.tab(PAYMENTS).values()) == 2
    assert run.config_path.read_text(encoding="utf-8") == before


REST = (
    f'[sources.razorpay]\nmode = "test"\naccount = "{TEST_ACCOUNT}"\n\n'
    f'[sinks.sheets]\nspreadsheet_id = "{TEST_SPREADSHEET_ID}"\n'
)
"""The config below its `[service]` table."""
PROFILE_SET = {
    "the value replaced, its comment kept": (
        '[service]\nretry_profile = "production"        # or "demo"\ndata_dir = "data"\n\n' + REST,
        '[service]\nretry_profile = "demo"        # or "demo"\ndata_dir = "data"\n\n' + REST,
    ),
    "a line added, with the file's own line endings": (
        ('[service]\ndata_dir = "data"\n\n' + REST).replace("\n", "\r\n"),
        ('[service]\nretry_profile = "demo"\ndata_dir = "data"\n\n' + REST).replace("\n", "\r\n"),
    ),
    "no service table": (REST, '[service]\nretry_profile = "demo"\n\n' + REST),
    "the service table is the last line, with no newline": (
        REST + "\n[service]",
        REST + '\n[service]\nretry_profile = "demo"\n',
    ),
    "a quoted table name": (
        '["service"]\ndata_dir = "data"\n\n' + REST,
        '["service"]\nretry_profile = "demo"\ndata_dir = "data"\n\n' + REST,
    ),
    "a dotted key at the root": (
        'service.data_dir = "data"\n\n' + REST,
        'service.retry_profile = "demo"\nservice.data_dir = "data"\n\n' + REST,
    ),
}
INLINE_SERVICE = 'service = { data_dir = "data" }\n\n' + REST
"""A valid config whose `service` table a line can't be added to."""


@pytest.mark.parametrize(("before", "after"), PROFILE_SET.values(), ids=PROFILE_SET.keys())
def test_the_demo_retry_profile_changes_one_line_and_the_config_still_loads(
    tmp_path: Path, before: str, after: str
) -> None:
    path = tmp_path / "webhook-sync.toml"
    path.write_text(before, encoding="utf-8", newline="")

    changed = with_demo_retry_profile(path)

    assert changed == after
    assert path.read_text(encoding="utf-8", newline="") == before  # nothing written yet
    path.write_text(changed, encoding="utf-8", newline="")
    assert load_settings(path, fixture_secrets()).service.retry_profile == "demo"


def test_a_config_the_profile_cannot_be_set_in_is_refused_untouched(tmp_path: Path) -> None:
    path = tmp_path / "webhook-sync.toml"
    path.write_text(INLINE_SERVICE, encoding="utf-8", newline="")

    with pytest.raises(CommandFailed) as refused:
        with_demo_retry_profile(path)

    assert 'set retry_profile = "demo" in [service] of webhook-sync.toml by hand' in str(
        refused.value
    )
    assert refused.value.exit_code == 2
    assert path.read_text(encoding="utf-8", newline="") == INLINE_SERVICE


def test_reset_refuses_before_it_empties_anything_when_it_cannot_set_the_profile(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "webhook-sync.toml"
    before = INLINE_SERVICE + ON_THE_DEMO_LIST
    config_path.write_text(before, encoding="utf-8", newline="")

    code, output = demo(config_path, "reset", fixture_environment())

    assert code == 2
    assert 'set retry_profile = "demo" in [service] of webhook-sync.toml by hand' in output
    assert not (tmp_path / "data").exists()
    assert config_path.read_text(encoding="utf-8", newline="") == before


def test_reset_refuses_before_it_empties_anything_when_the_config_is_read_only(
    tmp_path: Path,
) -> None:
    config_path = write_serve_config(tmp_path, free_port(), extra=ON_THE_DEMO_LIST)
    before = config_path.read_text(encoding="utf-8", newline="")
    config_path.chmod(stat.S_IREAD)
    try:
        if os.access(config_path, os.W_OK):
            pytest.skip("this user writes a read-only file (root)")

        code, output = demo(config_path, "reset", fixture_environment())
    finally:
        config_path.chmod(stat.S_IREAD | stat.S_IWRITE)

    assert code == 2
    assert 'set retry_profile = "demo" in [service] of webhook-sync.toml by hand' in output
    assert "cannot be written" in output
    assert "Traceback" not in output
    assert not (tmp_path / "data").exists()
    assert config_path.read_text(encoding="utf-8", newline="") == before


# -- demo reset and the tabs that are ours ---------------------------------------------------


def persons_payments_tab(sheets: FakeSheets) -> Tab:
    """A person's own tab titled Payments, with their header and a row."""
    sheets.apply([{"addSheet": {"properties": {"title": PAYMENTS}}}])
    theirs = sheets.tab(PAYMENTS)
    sheets.apply(
        [
            edit_cell(theirs.sheet_id, 0, 0, "Date"),
            edit_cell(theirs.sheet_id, 0, 1, "Customer"),
            edit_cell(theirs.sheet_id, 1, 0, "2026-10-04"),
            edit_cell(theirs.sheet_id, 1, 1, "Asha"),
        ]
    )
    return copy.deepcopy(sheets.tab(PAYMENTS))


async def test_reset_empties_a_tab_of_ours_a_person_renamed(
    run: CrashRun, read_rows: ReadRows
) -> None:
    await swept_in(run, "pay_DemoReset0001")
    ours = run.sheets.tab(PAYMENTS).sheet_id
    run.sheets.apply([rename_tab(ours, "Sales 2026")])

    code, output = await wsync_demo(run, "reset")

    assert code == 0, output
    assert not run.sheets.has_tab(ours)
    assert not run.sheets.has_tab("Sales 2026")
    assert run.sheets.tab(PAYMENTS).values() == [[c.header for c in payment_columns(run.config)]]
    assert await read_rows("SELECT * FROM payments") == []


async def test_reset_refuses_a_persons_tab_with_one_of_our_titles_and_leaves_all_alone(
    run: CrashRun, read_rows: ReadRows
) -> None:
    await swept_in(run, "pay_DemoReset0001")
    ours = run.sheets.tab(PAYMENTS).sheet_id
    run.sheets.apply([rename_tab(ours, "Sales 2026")])
    theirs = persons_payments_tab(run.sheets)
    before = run.config_path.read_text(encoding="utf-8")

    code, output = await wsync_demo(run, "reset")

    assert code == 2
    assert "a tab named Payments is not ours: its header differs" in output
    assert run.sheets.tab(PAYMENTS).cells == theirs.cells
    assert len(run.sheets.tab(ours).values()) == 2  # our header and the row, under its new name
    assert [row["payment_id"] for row in await read_rows("SELECT payment_id FROM payments")] == [
        "pay_DemoReset0001"
    ]
    assert run.config_path.read_text(encoding="utf-8") == before


async def test_reset_refuses_a_persons_tab_on_a_sheet_no_flush_has_written(
    run: CrashRun,
) -> None:
    theirs = persons_payments_tab(run.sheets)

    code, output = await wsync_demo(run, "reset")

    assert code == 2
    assert "a tab named Payments is not ours: its header differs" in output
    assert run.sheets.tab(PAYMENTS).cells == theirs.cells
    assert not run.sheets.has_tab(DAILY_SUMMARY)


async def test_a_refused_sheet_write_leaves_the_database_the_sheet_and_the_config_as_they_were(
    run: CrashRun, read_rows: ReadRows
) -> None:
    await swept_in(run, "pay_DemoReset0001")
    ours = run.sheets.tab(PAYMENTS).sheet_id
    run.sheets.apply([rename_tab(ours, "Sales 2026")])
    stored = await read_rows("SELECT payments_tab_id FROM sheet_state")
    before = run.config_path.read_text(encoding="utf-8")
    run.sheets.access = "view"  # shared as a Viewer: both reads pass, the write gets a 403

    code, output = await wsync_demo(run, "reset")

    assert code == 1
    assert "the spreadsheet is not shared with the service account" in output
    assert "as an Editor" in output
    assert "emptying the database" not in output
    assert await read_rows("SELECT payments_tab_id FROM sheet_state") == stored
    assert [row["payment_id"] for row in await read_rows("SELECT payment_id FROM payments")] == [
        "pay_DemoReset0001"
    ]
    assert len(run.sheets.tab(ours).values()) == 2
    assert run.config_path.read_text(encoding="utf-8") == before

    run.sheets.access = "edit"  # shared as an Editor again: the stored ids still name our tab
    again, output = await wsync_demo(run, "reset")

    assert again == 0, output
    assert not run.sheets.has_tab(ours)
    assert await read_rows("SELECT * FROM payments") == []


# -- stopping the receiver -------------------------------------------------------------------


def a_process(seconds: int) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def test_a_receiver_that_does_not_stop_in_time_is_killed(
    data_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deaf = a_process(20)  # asked to stop, it carries on

    stop_receiver(deaf, data_dir, within=0.5, ask=lambda process: None)

    assert deaf.poll() is not None
    assert "the receiver did not stop in 0.5 s and was killed" in capsys.readouterr().out


def test_a_lock_still_held_after_the_receiver_stopped_fails_the_scene(data_dir: Path) -> None:
    gone = a_process(0)

    with hold_lock(data_dir), pytest.raises(CommandFailed) as failed:
        stop_receiver(gone, data_dir, within=0.5)

    assert "the lock is still held" in str(failed.value)


# -- sweeping until each payment is stored ---------------------------------------------------

MADE_AT = 1_791_195_000
PAID = {"pay_Tj9rKBOXWcsMeE": MADE_AT, "pay_Tj9rKBOXWcsMeF": MADE_AT + 20}
"""Two payments and when Razorpay made each, 20 s apart."""


class Sweeps:
    """`sweep_until_stored` on a clock of its own, Razorpay's too: the payments are made as it
    starts, each sweep finishes and takes two seconds, each wait takes what it asks, and
    Razorpay's list shows the payments from `listed_at` seconds on (never, when None). A sweep
    stores a listed payment while its slices still read that far back, and a re-list stores it
    whenever it is listed."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, listed_at: float | None) -> None:
        self.now = 0.0
        self.listed_at = listed_at
        self.stored = False
        self.relists: list[float] = []
        """When each re-list was asked for."""
        monkeypatch.setattr(demo_scenes.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(demo_scenes.time, "sleep", self._pass)
        monkeypatch.setattr(demo_scenes, "run_by_hand", self._sweep)
        monkeypatch.setattr(demo_scenes, "_stored", self._stored)

    def _pass(self, seconds: float) -> None:
        self.now += seconds

    def _sweep(
        self, config_path: Path, path: str, body: dict[str, bool] | None, work: object
    ) -> tuple[dict[str, object], str]:
        self.now += 2.0
        relist = bool(body and body["relist"])
        if relist:
            self.relists.append(self.now)
        checkpoint = MADE_AT + self.now - SETTLE.total_seconds()
        read_by_slices = checkpoint - OVERLAP.total_seconds() <= max(PAID.values())
        listed = self.listed_at is not None and self.now >= self.listed_at
        self.stored = self.stored or (listed and (relist or read_by_slices))
        at = datetime.fromtimestamp(checkpoint, UTC)
        sweeps = [
            {"list": name, "checkpoint_at": to_db_text(at)} for name in ("payments", "refunds")
        ]
        return {"finished": True, "sweeps": sweeps}, ""

    async def _stored(self, data_dir: Path, paid: list[str]) -> set[str]:
        return set(paid) if self.stored else set()


def test_payments_razorpay_lists_nine_minutes_late_are_stored_by_a_sweep_and_the_scene_goes_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sweeps = Sweeps(monkeypatch, listed_at=9 * 60.0)

    demo_scenes.sweep_until_stored(tmp_path / "webhook-sync.toml", tmp_path, PAID)

    assert 9 * 60.0 <= sweeps.now < 9 * 60.0 + 10
    assert sweeps.relists == []
    assert "swept: all 2 payments stored" in capsys.readouterr().out


def test_payments_razorpay_lists_after_the_sweeps_overlap_are_stored_by_a_re_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sweeps = Sweeps(monkeypatch, listed_at=29 * 60.0)  # as on 2026-10-05 (razorpay-list-facts.md)

    demo_scenes.sweep_until_stored(tmp_path / "webhook-sync.toml", tmp_path, PAID)

    tolerance = (OVERLAP + SETTLE).total_seconds()
    assert 29 * 60.0 <= sweeps.now < 29 * 60.0 + 30
    assert tolerance < sweeps.relists[0] < tolerance + 30  # once no sweep reads that far back
    printed = capsys.readouterr().out
    assert "no sweep reads that far back" in printed
    assert "re-listing" in printed
    assert "swept: all 2 payments stored" in printed


def test_payments_razorpay_never_lists_fail_the_scene_only_after_its_longer_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sweeps = Sweeps(monkeypatch, listed_at=None)

    with pytest.raises(CommandFailed) as failed:
        demo_scenes.sweep_until_stored(tmp_path / "webhook-sync.toml", tmp_path, PAID)

    assert demo_scenes.STORED_WITHIN >= 2 * 54 * 60  # twice the longest measured
    assert demo_scenes.STORED_WITHIN < sweeps.now < demo_scenes.STORED_WITHIN + 60
    line = str(failed.value)
    assert "2 of 2 payments" in line
    assert "Razorpay's payments list has not shown them" in line
    assert "hourly re-list" in line


# -- demo verify -----------------------------------------------------------------------------


async def test_verify_prints_the_full_verify_line_on_its_own_and_through_a_server(
    run: CrashRun,
) -> None:
    await swept_in(run, "pay_DemoVerify0001", "pay_DemoVerify0002")

    alone_code, alone = await wsync_demo(run, "verify")
    async with run.server():
        served_code, served = await wsync_demo(run, "verify")

    for code, output in ((alone_code, alone), (served_code, served)):
        assert code == 0, output
        assert "running a full verify" in output
        assert re.search(r"Verified .*: last 30 days, 2 payments, 0 differences", output), output
    assert "(on the database: no server is running)" in alone
    assert "(through the running server)" in served


# -- demo outage -----------------------------------------------------------------------------

TESTS = Path(__file__).parent
PAGE_URL = re.compile(r"http://127\.0\.0\.1:\d+/pay/(order_\w+)")
SCENE_LIMIT = 120.0
"""Real seconds a scene gets to end."""
LEFT_RUNNING = re.compile(r"the receiver is running in its normal mode at (\S+), pid (\d+)")


def kill_tree(pid: int) -> None:
    """Kill a process and everything under it; one started in a session of its own is the
    leader of its group."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)


@pytest.fixture(autouse=True)
def no_scene_left() -> Iterator[None]:
    """Every scene a test started is killed when it ends, and so is any receiver a scene
    left running: a failed test leaves none holding the port and the database's lock."""
    yield
    while Scene.started:
        Scene.started.pop().kill()


class Scene:
    """A `wsync demo` subprocess, its lines read as it prints them."""

    started: ClassVar[list[Scene]] = []
    """The scenes started and not yet killed."""

    def __init__(self, run: CrashRun, scene: str, *args: str, payer: str) -> None:
        Scene.started.append(self)
        environ = run.environ | {PAYER_VARIABLE: payer, "PYTHONPATH": str(TESTS)}
        self.process = subprocess.Popen(
            [wsync_path(), "demo", scene, *args, "--config", str(run.config_path)],
            env=environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            # A group of its own, so that `interrupt` reaches the scene and not the tests.
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            start_new_session=sys.platform != "win32",
        )
        self.lines: list[str] = []
        self._new: queue.Queue[str | None] = queue.Queue()
        self._closed = False
        """Whether the end of the output has been read."""
        self._killed: set[int] = set()
        """The left-running receivers already killed, by pid: none is killed twice, as a pid
        can be reused."""
        Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self._new.put(line.rstrip("\n"))
        self._new.put(None)

    async def until(self, text: str, *, within: float = SCENE_LIMIT) -> str:
        """The first line from now on that holds `text`."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            try:
                line = await asyncio.to_thread(self._new.get, True, 0.5)
            except queue.Empty:
                continue
            if line is None:
                self._closed = True
                break
            self.lines.append(line)
            if text in line:
                return line
        pytest.fail(f"no line held {text!r}:\n" + "\n".join(self.lines))

    def kill(self) -> None:
        """Kill the scene's processes while they run, then any receiver it said it left
        running."""
        if self.process.poll() is None:  # a reaped pid may be someone else's by now
            kill_tree(self.process.pid)
            self.process.wait(timeout=30)
        while not self._closed:  # its last lines, until the pipe closes
            try:
                line = self._new.get(timeout=5)
            except queue.Empty:
                break
            if line is None:
                self._closed = True
            else:
                self.lines.append(line)
        self.kill_left_running()

    def kill_left_running(self) -> None:
        """Kill each receiver the scene said it left running, once."""
        for found in LEFT_RUNNING.finditer("\n".join(self.lines)):
            pid = int(found[2])
            if pid not in self._killed:
                self._killed.add(pid)
                kill_tree(pid)

    def interrupt(self) -> None:
        """Ctrl+C, as the owner presses it; on Windows, Ctrl+Break: a console sends Ctrl+C
        to no process group but its own."""
        if sys.platform == "win32":
            os.kill(self.process.pid, signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(self.process.pid, signal.SIGINT)

    async def ended(self, *, within: float = SCENE_LIMIT) -> tuple[int, str]:
        try:
            code = await asyncio.to_thread(self.process.wait, within)
        except subprocess.TimeoutExpired:
            stop(self.process)
            pytest.fail("the scene did not end:\n" + "\n".join(self.lines))
        while (line := await asyncio.to_thread(self._new.get)) is not None:
            self.lines.append(line)
        self._closed = True
        output = "\n".join(self.lines)
        assert [value for value in SECRET_VALUES if value in output] == []
        return code, output


async def settled_at_razorpay(run: CrashRun, scene: Scene) -> None:
    """Once the sweep finds the payments too new to list, Razorpay's clock moves two minutes
    on, as it would while the scene waits."""
    await scene.until("not listed yet")
    run.razorpay.now += timedelta(minutes=2)


async def assert_swept_once(run: CrashRun, read_rows: ReadRows, paid: list[str]) -> None:
    """Each payment has one ledger row and one sheet row with Via = sweep, and no delivery."""
    assert await read_rows("SELECT payment_id, via FROM payments ORDER BY payment_id") == [
        {"payment_id": payment_id, "via": "sweep"} for payment_id in sorted(paid)
    ]
    assert {row["origin"] for row in await read_rows("SELECT origin FROM events")} == {"sweep"}
    header, *rows = run.sheets.tab(PAYMENTS).values()
    via = header.index("Via")
    assert sorted((row[0], row[via]) for row in rows) == [(p, "sweep") for p in sorted(paid)]


async def test_outage_pays_orders_while_stopped_and_the_sweep_brings_each_back_once(
    run: CrashRun, read_rows: ReadRows
) -> None:
    scene = Scene(run, "outage", "--orders", "2", payer=FAKE_PAYER)
    await settled_at_razorpay(run, scene)

    code, output = await scene.ended()

    assert code == 0, output
    paid = run.razorpay.orders_paid()
    assert len(paid) == 2
    assert [order["amount"] for order in run.razorpay.orders.values()] == [50000, 50000]
    assert "the receiver is stopped" in scene.lines[0]
    for order_id, payment_id in zip(run.razorpay.orders, paid, strict=True):
        assert f"{order_id} made for INR 500.00" in output
        assert f"{payment_id} captured" in output
        assert f"{payment_id}: one ledger row and one sheet row, Via = sweep" in output
    assert "waiting for Razorpay to capture the payment" in output
    assert "starting the receiver" in output
    assert scene.lines[-1] == "the receiver is stopped"
    await assert_swept_once(run, read_rows, paid)
    receiver_log = (run.config.service.data_dir / "demo-serve.log").read_text(encoding="utf-8")
    slices = [
        line
        for line in map(json.loads, receiver_log.splitlines())
        if line["message"] == "sweep slice" and line["list"] == "payments"
    ]
    assert sum(line["listed"] for line in slices) == 2
    assert [value for value in SECRET_VALUES if value in receiver_log] == []


async def test_outage_re_lists_a_payment_razorpay_shows_only_after_the_sweeps_overlap(
    run: CrashRun, read_rows: ReadRows
) -> None:
    # As measured on 2026-10-05: the list showed a payment about half an hour after it was
    # made, its fetch and its order's payments at once (razorpay-list-facts.md, Q7).
    run.razorpay.listed_late = timedelta(minutes=25)
    scene = Scene(run, "outage", "--orders", "1", payer=FAKE_PAYER)

    await scene.until("not listed yet")
    run.razorpay.now += timedelta(minutes=12)  # a sweep moves the checkpoint past the overlap
    await scene.until("no sweep reads that far back again; re-listing")
    run.razorpay.now += timedelta(minutes=14)  # 26 minutes on, the list shows it
    code, output = await scene.ended()

    assert code == 0, output
    [payment_id] = run.razorpay.orders_paid()
    assert f"{payment_id}: one ledger row and one sheet row, Via = sweep" in output
    await assert_swept_once(run, read_rows, [payment_id])
    receiver_log = (run.config.service.data_dir / "demo-serve.log").read_text(encoding="utf-8")
    logged = [json.loads(line) for line in receiver_log.splitlines()]
    of_payments = [line for line in logged if line.get("list") == "payments"]
    assert sum(line["stored"] for line in of_payments if line["message"] == "sweep slice") == 0
    assert [line["stored"] for line in of_payments if line["message"] == "re-list"] == [1]


async def test_when_the_payer_fails_outage_prints_the_page_to_pay_by_hand_and_waits(
    run: CrashRun, read_rows: ReadRows
) -> None:
    scene = Scene(run, "outage", "--orders", "1", payer=FAILING_PAYER)

    line = await scene.until("pay it by hand at")
    found = PAGE_URL.search(line)
    assert found is not None, line
    page = await asyncio.to_thread(httpx.get, found[0], trust_env=False)
    await scene.until("waiting for Razorpay to capture the payment")
    payment_id = run.razorpay.pay(found[1])  # a person pays at the page
    await settled_at_razorpay(run, scene)
    code, output = await scene.ended()

    assert code == 0, output
    assert "Checkout showed a captcha" in line
    assert page.status_code == 200
    assert found[1] in page.text
    assert CHECKOUT_JS in page.text
    assert f"{payment_id}: one ledger row and one sheet row, Via = sweep" in output
    await assert_swept_once(run, read_rows, [payment_id])


async def test_outage_waits_out_a_429_and_sweeps_again_when_a_sweep_stops_short(
    run: CrashRun, read_rows: ReadRows
) -> None:
    run.razorpay.orders_busy = 1
    run.razorpay.lists_down = True
    scene = Scene(run, "outage", "--orders", "1", payer=FAKE_PAYER)

    await scene.until("the sweep stopped before catching up")
    run.razorpay.lists_down = False
    run.razorpay.now += timedelta(minutes=2)
    code, output = await scene.ended()

    assert code == 0, output
    assert run.razorpay.orders_busy == 0
    assert "Razorpay answered 429 (too many requests)" in output
    await assert_swept_once(run, read_rows, run.razorpay.orders_paid())


async def test_outage_refuses_while_a_server_runs(run: CrashRun) -> None:
    async with run.server():
        code, output = await wsync_demo(run, "outage")

    assert code == 2
    assert "a server is running: stop wsync serve first" in output
    assert run.razorpay.orders == {}


# -- demo duplicates ---------------------------------------------------------------------------


@contextmanager
def left_running(run: CrashRun, scene: Scene) -> Generator[httpx.AsyncClient]:
    """A client for the receiver the scene left running; stopped when the block ends. The
    test reads the database inside the block: on Windows a stopped server's files may stay
    held a moment longer (the crash harness)."""
    output = "\n".join(scene.lines)
    found = LEFT_RUNNING.search(output)
    try:
        assert found is not None, output
        assert found[1] == f"http://127.0.0.1:{run.port}"
        yield httpx.AsyncClient(base_url=found[1], trust_env=False, timeout=30)
    finally:
        scene.kill_left_running()


async def assert_in_normal_mode(run: CrashRun, client: httpx.AsyncClient, payment_id: str) -> None:
    """Healthy, and the event answered as it normally is, new or a duplicate: no switch
    answers it 500."""
    async with client:
        assert (await client.get("/health")).status_code == 200
        [sent] = await run.razorpay.deliver(client, run.razorpay.captured_delivery(payment_id))
    assert sent.status_code in (200, 202)


async def test_duplicates_answers_500_to_four_deliveries_and_keeps_one_row(
    run: CrashRun, read_rows: ReadRows
) -> None:
    scene = Scene(run, "duplicates", payer=FAKE_PAYER)
    await scene.until("waiting for Razorpay's deliveries")
    [payment_id] = run.razorpay.orders_paid()
    delivery = run.razorpay.captured_delivery(payment_id)
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{run.port}", trust_env=False, timeout=30
    ) as receiver:
        for attempt in range(1, 6):  # Razorpay's retries, one after another
            await run.razorpay.deliver(receiver, delivery)
            await scene.until(f"payment.captured attempt {attempt}:")
    code, output = await scene.ended()

    with left_running(run, scene) as client:
        assert code == 0, output
        assert [sent.status_code for sent in run.razorpay.sent] == [500, 500, 500, 500, 200]
        assert "starting the receiver: wsync serve --demo-fail-first 4" in output
        assert f"{payment_id} captured" in output
        assert "payment.captured attempt 1: answered 500 on purpose, stored as new" in output
        assert "payment.captured attempt 2: answered 500 on purpose, a duplicate" in output
        assert "payment.captured attempt 5: answered 200, a duplicate" in output
        assert f"{payment_id}: one ledger row and one sheet row; 4 duplicates" in output
        assert await read_rows("SELECT payment_id, via FROM payments") == [
            {"payment_id": payment_id, "via": "delivery"}
        ]
        _, *rows = run.sheets.tab(PAYMENTS).values()
        assert [row[0] for row in rows] == [payment_id]
        await assert_in_normal_mode(run, client, payment_id)


async def test_duplicates_with_no_delivery_names_the_tunnel_and_fails(run: CrashRun) -> None:
    scene = Scene(run, "duplicates", "--wait", "2", payer=FAKE_PAYER)

    code, output = await scene.ended()

    with left_running(run, scene) as client:
        assert code == 1, output
        [payment_id] = run.razorpay.orders_paid()
        assert f"no delivery of {payment_id} arrived in 2 s" in output
        assert "the public URL or the tunnel is the likely cause" in output
        assert "wsync doctor --url" in output
        await assert_in_normal_mode(run, client, payment_id)


def taken(port: int) -> socket.socket:
    """The port, listened on by the test so that no server can take it, even one that asks
    to reuse the address (Windows lets it, unless the port is held exclusively)."""
    held = socket.socket()
    if sys.platform == "win32":
        held.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    held.bind(("127.0.0.1", port))
    held.listen()
    return held


async def test_a_receiver_that_will_not_start_after_the_scene_leaves_its_error_and_exit_code(
    run: CrashRun,
) -> None:
    scene = Scene(run, "duplicates", "--wait", "2", payer=FAKE_PAYER)
    await scene.until("the receiver is stopped")
    with taken(run.port):  # before the receiver in its normal mode starts
        code, output = await scene.ended()

    assert code == 1, output
    assert "the public URL or the tunnel is the likely cause" in output
    assert "the receiver in its normal mode did not start: the receiver stopped as it" in output
    assert LEFT_RUNNING.search(output) is None


async def test_an_interrupted_duplicates_leaves_the_receiver_in_its_normal_mode(
    run: CrashRun,
) -> None:
    scene = Scene(run, "duplicates", payer=FAKE_PAYER)
    await scene.until("waiting for Razorpay's deliveries")

    scene.interrupt()
    code, output = await scene.ended()

    with left_running(run, scene) as client:
        assert code != 0, output
        assert "interrupted" in output
        [payment_id] = run.razorpay.orders_paid()
        await assert_in_normal_mode(run, client, payment_id)


async def test_duplicates_refuses_while_a_server_runs(run: CrashRun) -> None:
    async with run.server():
        code, output = await wsync_demo(run, "duplicates")

    assert code == 2
    assert "a server is running: stop wsync serve first" in output
    assert run.razorpay.orders == {}


# -- what the default suite leaves out ---------------------------------------------------------


def test_the_default_run_leaves_the_live_test_out() -> None:
    assert runs_live("live")
    assert not runs_live("")
    assert not runs_live("not posix")
    assert not runs_live("not live")


def test_wsync_and_its_demo_load_no_browser_until_the_browser_payer_pays() -> None:
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, webhook_sync.cli.main, webhook_sync.demo.payer; "
            "print(sorted(m for m in sys.modules if m.split('.')[0] in ('playwright', 'pyee')))",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )

    assert loaded.stdout.strip() == "[]"
