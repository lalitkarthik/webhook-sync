"""`wsync demo reset`, `outage` and `verify` as real subprocesses (seam 3; Q105, Q120, Q181).

Razorpay and Google are FakeRazorpay and FakeSheets over HTTP on 127.0.0.1, as in the command
tests of P4 and the crash tests of P5, and the payer is a fake one (`fakes.payer`) the command
loads by `WSYNC_DEMO_PAYER`: it pays an order through FakeRazorpay's checkout call, where the
real payer drives a browser. What comes out is what the owner sees: the printed lines, the exit
code, the ledger, the sheet and the config file.
"""

import asyncio
import queue
import re
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from threading import Thread

import httpx
import pytest
from conftest import runs_live
from crash.harness import CrashRun, served_run
from fakes.payer import FAILING_PAYER, FAKE_PAYER
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

from webhook_sync.cli.demo import set_demo_retry_profile
from webhook_sync.config import load_settings
from webhook_sync.demo.payer import CHECKOUT_JS, PAYER_VARIABLE
from webhook_sync.sinks.encoding import payment_columns
from webhook_sync.sinks.sheets import (
    DAILY_SUMMARY,
    DAILY_SUMMARY_HEADER,
    PAYMENTS,
    SYNC_STATUS,
    SYNC_STATUS_HEADER,
)

SCENES = ("reset", "outage", "verify")
LIVE_KEY_ID = "rzp_live_fixtureKeyId01"
SECRET_VALUES = (TEST_KEY_ID, TEST_KEY_SECRET, TEST_WEBHOOK_SECRET, LIVE_KEY_ID)
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


@pytest.mark.parametrize(
    ("service", "newline"),
    [
        ('[service]\nretry_profile = "production"  # or "demo"\ndata_dir = "data"\n', "\n"),
        ('[service]\r\ndata_dir = "data"\r\n', "\r\n"),
        ("", "\n"),
    ],
)
def test_the_demo_retry_profile_replaces_or_adds_the_one_line(
    tmp_path: Path, service: str, newline: str
) -> None:
    rest = (
        f'[sources.razorpay]\nmode = "test"\naccount = "{TEST_ACCOUNT}"\n\n'
        f'[sinks.sheets]\nspreadsheet_id = "{TEST_SPREADSHEET_ID}"\n'
    ).replace("\n", newline)
    path = tmp_path / "webhook-sync.toml"
    path.write_text(service + newline + rest, encoding="utf-8", newline="")

    set_demo_retry_profile(path)

    text = path.read_text(encoding="utf-8", newline="")
    assert text.count("retry_profile") == 1
    assert text.endswith(rest)
    assert load_settings(path, fixture_secrets()).service.retry_profile == "demo"


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


class Scene:
    """A `wsync demo` subprocess, its lines read as it prints them."""

    def __init__(self, run: CrashRun, scene: str, *args: str, payer: str) -> None:
        environ = run.environ | {PAYER_VARIABLE: payer, "PYTHONPATH": str(TESTS)}
        self.process = subprocess.Popen(
            [wsync_path(), "demo", scene, *args, "--config", str(run.config_path)],
            env=environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
        )
        self.lines: list[str] = []
        self._new: queue.Queue[str | None] = queue.Queue()
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
                break
            self.lines.append(line)
            if text in line:
                return line
        pytest.fail(f"no line held {text!r}:\n" + "\n".join(self.lines))

    async def ended(self, *, within: float = SCENE_LIMIT) -> tuple[int, str]:
        try:
            code = await asyncio.to_thread(self.process.wait, within)
        except subprocess.TimeoutExpired:
            stop(self.process)
            pytest.fail("the scene did not end:\n" + "\n".join(self.lines))
        while (line := await asyncio.to_thread(self._new.get)) is not None:
            self.lines.append(line)
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


async def test_outage_sweeps_again_when_a_sweep_stops_short(
    run: CrashRun, read_rows: ReadRows
) -> None:
    run.razorpay.lists_down = True
    scene = Scene(run, "outage", "--orders", "1", payer=FAKE_PAYER)

    await scene.until("the sweep stopped before catching up")
    run.razorpay.lists_down = False
    run.razorpay.now += timedelta(minutes=2)
    code, output = await scene.ended()

    assert code == 0, output
    await assert_swept_once(run, read_rows, run.razorpay.orders_paid())


async def test_outage_refuses_while_a_server_runs(run: CrashRun) -> None:
    async with run.server():
        code, output = await wsync_demo(run, "outage")

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
