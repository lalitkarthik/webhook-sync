"""Runs by hand through the admin listener, in process (Q152, A19, #43): `POST /admin/sweep`,
`/admin/backfill` and `/admin/verify` answer 202 with a run id, and `GET /admin/runs/{id}`
reports the run's progress and, once it is over, its result.

Seam: the admin app over the running public app. Razorpay's lists and fetches are answered by
respx from the recordings (the fakes' `FakeRazorpay`), Google by P3's fake Sheets. The
background sweep is kept out of the way, so only the run by hand lists. What comes out is what
an outsider sees: the run's answers, events with their origin, checkpoints and `verify_runs`.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any, override

import httpx
import pytest
import respx
from fakes.razorpay import (
    RAZORPAY_API,
    FakeRazorpay,
    recorded_listed_payments,
    recorded_listed_refunds,
)
from fakes.sheets import FakeSheets
from support import RECORDED_ACCOUNT, TEST_SPREADSHEET_ID, ReadRows
from test_sweeper import NOW
from time_machine import TimeMachineFixture

from webhook_sync.admin.app import create_admin_app
from webhook_sync.config import Config, SweeperConfig
from webhook_sync.queue import runner as runner_module
from webhook_sync.receiver.app import create_app, shut_down
from webhook_sync.sweeper import Sweeper

SWEPT = ("pay_Tj64grZUMO3PEj", "pay_Tj66x2JuEXjIdB", "pay_Tj9gmEfed6kRqO", "pay_Tj9rKBOXWcsMeE")
FAILED = "pay_Tj9sm13R8h2378"
LISTED = sorted((*SWEPT, FAILED))
"""Every recorded payment but the two never attempted (`created`): what a list stores."""


@pytest.fixture(autouse=True)
def clock(time_machine: TimeMachineFixture) -> None:
    time_machine.move_to(NOW, tick=True)


@pytest.fixture(autouse=True)
def quiet_loops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The background sweep never runs, so only a run by hand lists; idle waits short."""

    async def never(self: Sweeper) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(Sweeper, "run", never)
    monkeypatch.setattr(runner_module, "IDLE_WAIT", timedelta(milliseconds=10))


@dataclass
class HeldRazorpay(FakeRazorpay):
    """The fake whose lists wait, while `holding`, until `release` is set."""

    holding: bool = False
    listed: asyncio.Event = field(default_factory=asyncio.Event)
    """Set once a list call has arrived."""

    @override
    async def _list(self, request: httpx.Request, items: list[dict[str, Any]]) -> httpx.Response:
        self.listed.set()
        if self.holding:
            await self.release.wait()
        return await super()._list(request, items)


@pytest.fixture
def razorpay() -> Iterator[HeldRazorpay]:
    fake = HeldRazorpay(
        now=NOW, payments=recorded_listed_payments(), refunds=recorded_listed_refunds()
    )
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        FakeSheets(TEST_SPREADSHEET_ID).mount(router)
        yield fake


@pytest.fixture
def run_config(config: Config) -> Config:
    return replace(
        config,
        razorpay=replace(config.razorpay, account=RECORDED_ACCOUNT, api_url=RAZORPAY_API),
        sweeper=SweeperConfig(initial_lookback_days=1),
    )


@asynccontextmanager
async def admin_listener(config: Config, data_dir: Path) -> AsyncGenerator[httpx.AsyncClient]:
    public = create_app(config, data_dir)
    async with public.router.lifespan_context(public):
        transport = httpx.ASGITransport(app=create_admin_app(public, data_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            yield client


async def started(admin: httpx.AsyncClient, path: str, body: Any = None) -> str:
    answer = await admin.post(path, json=body)
    assert answer.status_code == 202, answer.text
    return answer.json()["run_id"]


async def over(admin: httpx.AsyncClient, run_id: str) -> dict[str, Any]:
    """The run's report once it is no longer running."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        report = (await admin.get(f"/admin/runs/{run_id}")).json()
        if report["state"] != "running":
            return report
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} still running")


async def test_a_sweep_by_hand_reports_what_each_list_stored(
    razorpay: HeldRazorpay, run_config: Config, data_dir: Path, read_rows: ReadRows
) -> None:
    async with admin_listener(run_config, data_dir) as admin:
        run_id = await started(admin, "/admin/sweep")
        report = await over(admin, run_id)

    assert (report["id"], report["kind"], report["state"], report["error"]) == (
        run_id,
        "sweep",
        "finished",
        None,
    )
    result = report["result"]
    assert result["finished"] is True
    assert [(s["list"], s["stored"]) for s in result["sweeps"]] == [("payments", 5), ("refunds", 2)]
    assert all(s["last_sweep_at"] and s["checkpoint_at"] for s in result["sweeps"])
    events = await read_rows("SELECT payment_key FROM events WHERE origin = 'sweep'")
    assert sorted({e["payment_key"] for e in events}) == LISTED


async def test_a_backfill_by_hand_stores_backfill_events_and_moves_no_checkpoint(
    razorpay: HeldRazorpay, run_config: Config, data_dir: Path, read_rows: ReadRows
) -> None:
    async with admin_listener(run_config, data_dir) as admin:
        run_id = await started(admin, "/admin/backfill", {"from": "2026-10-02", "to": "2026-10-03"})
        report = await over(admin, run_id)

    assert (report["kind"], report["state"]) == ("backfill", "finished")
    assert report["result"] == {"from": "2026-10-02", "to": "2026-10-03", "stored": 5}
    events = await read_rows("SELECT event_id, origin, payment_key FROM events")
    assert {e["origin"] for e in events} == {"backfill"}
    assert all(e["event_id"].startswith(f"backfill:{e['payment_key']}:") for e in events)
    assert sorted(e["payment_key"] for e in events) == LISTED
    assert await read_rows("SELECT list FROM checkpoints") == []


async def test_a_backfill_whose_dates_run_backwards_is_refused(
    razorpay: HeldRazorpay, run_config: Config, data_dir: Path
) -> None:
    async with admin_listener(run_config, data_dir) as admin:
        answer = await admin.post(
            "/admin/backfill", json={"from": "2026-10-03", "to": "2026-10-02"}
        )

    assert answer.status_code == 422


async def test_a_full_verify_reports_running_until_it_ends_then_its_findings(
    razorpay: HeldRazorpay, run_config: Config, data_dir: Path, read_rows: ReadRows
) -> None:
    razorpay.holding = True
    async with admin_listener(run_config, data_dir) as admin:
        run_id = await started(admin, "/admin/verify", {"full": True, "days": 3})
        await asyncio.wait_for(razorpay.listed.wait(), 30)
        while_held = (await admin.get(f"/admin/runs/{run_id}")).json()
        razorpay.release.set()
        report = await over(admin, run_id)

    assert (while_held["state"], while_held["result"], while_held["finished_at"]) == (
        "running",
        None,
        None,
    )
    assert (report["kind"], report["state"]) == ("verify", "finished")
    assert report["finished_at"] is not None
    result = report["result"]
    [run] = await read_rows("SELECT id, kind, window_days, status_line FROM verify_runs")
    assert (result["verify_run_id"], result["kind"], result["line"]) == (
        run["id"],
        "full",
        run["status_line"],
    )
    assert run["window_days"] == 3
    assert "last 3 days, 5 payments, 5 differences" in result["line"]
    assert (result["payments_checked"], result["findings_count"], result["repaired_count"]) == (
        5,
        5,
        0,
    )
    assert sorted(result["findings"], key=lambda f: f["payment_id"]) == [
        {"kind": "missing", "payment_id": payment_id, "detail": None, "repaired": False}
        for payment_id in LISTED
    ]
    [payments_from] = [int(q["from"]) for q in razorpay.calls_to("/v1/payments")]
    assert abs(payments_from - (NOW - timedelta(days=3)).timestamp()) < 60


async def test_a_full_verify_asked_to_repair_queues_a_ledger_job_per_finding(
    razorpay: HeldRazorpay, run_config: Config, data_dir: Path, read_rows: ReadRows
) -> None:
    async with admin_listener(run_config, data_dir) as admin:
        run_id = await started(admin, "/admin/verify", {"full": True, "repair": True})
        report = await over(admin, run_id)

    result = report["result"]
    assert result["repaired_count"] == 5
    assert {f["repaired"] for f in result["findings"]} == {True}
    findings = await read_rows("SELECT repair_job_id FROM verify_findings")
    assert all(f["repair_job_id"] is not None for f in findings)


async def test_a_quick_verify_with_no_sheet_written_yet_fails_saying_why(
    razorpay: HeldRazorpay, run_config: Config, data_dir: Path
) -> None:
    async with admin_listener(run_config, data_dir) as admin:
        run_id = await started(admin, "/admin/verify", {})
        report = await over(admin, run_id)

    assert (report["kind"], report["state"], report["result"]) == ("verify", "failed", None)
    assert "nothing to check" in report["error"]


async def test_an_unknown_run_is_404(
    razorpay: HeldRazorpay, run_config: Config, data_dir: Path
) -> None:
    async with admin_listener(run_config, data_dir) as admin:
        answer = await admin.get("/admin/runs/no-such-run")

    assert answer.status_code == 404


async def test_a_run_asked_for_once_the_shutdown_has_begun_is_refused(
    razorpay: HeldRazorpay, run_config: Config, data_dir: Path
) -> None:
    public = create_app(run_config, data_dir)
    async with public.router.lifespan_context(public):
        transport = httpx.ASGITransport(app=create_admin_app(public, data_dir))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as admin:
            await shut_down(public)  # the listeners still answer until the lifespan ends
            answers = [
                await admin.post("/admin/sweep"),
                await admin.post(
                    "/admin/backfill", json={"from": "2026-10-02", "to": "2026-10-02"}
                ),
                await admin.post("/admin/verify", json={"full": True}),
            ]

    assert [answer.status_code for answer in answers] == [503, 503, 503]
    assert razorpay.list_calls == []
