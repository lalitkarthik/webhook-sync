"""`wsync doctor` run in process (#60): the command as a person types it, its printed lines and
its exit code, against FakeRazorpay and FakeSheets through respx and a real database.

One line per check: `ok`, `warning` or `fail`, the check's name, and the reason in plain words.
"""

import asyncio
import os
import sqlite3
import stat
from collections.abc import Callable, Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, cast

import httpx
import pytest
import respx
from fakes.razorpay import RAZORPAY_API, WEBHOOK_URL, FakeRazorpay, listed_webhook
from fakes.sheets import TOKEN_URL, FakeSheets, edit_cell
from processes import free_port, write_serve_config
from support import RECORDED_ACCOUNT, TEST_SPREADSHEET_ID, fixture_secrets

from webhook_sync.cli.doctor import network_filesystem
from webhook_sync.cli.main import main
from webhook_sync.cli.serve import start
from webhook_sync.config import load_config
from webhook_sync.db.writer import Writer
from webhook_sync.receiver.app import sheets_client
from webhook_sync.sinks.sheets import PAYMENTS, Flusher

CHECKS = ["config", "secrets", "razorpay key", "razorpay webhook", "clock", "sheet", "database"]
LIVE_KEY_ID = "rzp_live_fixtureKeyId01"


@dataclass
class Setup:
    config_path: Path
    razorpay: FakeRazorpay
    sheets: FakeSheets
    router: respx.MockRouter

    @property
    def database(self) -> Path:
        return self.config_path.parent / "data" / "webhook-sync.db"

    def sql(self, statement: str) -> list[tuple[object, ...]]:
        with closing(sqlite3.connect(self.database)) as db, db:
            return db.execute(statement).fetchall()


@dataclass(frozen=True)
class Report:
    exit_code: int
    lines: dict[str, tuple[str, str]]
    """Each check's state and reason, by the check's name, in the order printed."""

    def state(self, check: str) -> str:
        return self.lines[check][0]

    def says(self, check: str) -> str:
        return self.lines[check][1]

    def others_ok(self, *but: str) -> bool:
        return all(state == "ok" for check, (state, _) in self.lines.items() if check not in but)


async def _first_flush(config_path: Path) -> None:
    """One flush with no jobs: the three tabs are made and their ids stored."""
    config = load_config(config_path)
    writer = await Writer.open(config.service.data_dir)
    try:
        async with httpx.AsyncClient() as http:
            await Flusher(writer, sheets_client(config, http), config).flush([])
    finally:
        await writer.close()


@contextmanager
def healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, config_dir: str = "conf"
) -> Generator[Setup]:
    """A setup with nothing wrong: the fixture secrets in the environment, a migrated database,
    the sheet's tabs made, a webhook listed at Razorpay, whose clock is ours."""
    for name, value in fixture_secrets().items():
        monkeypatch.setenv(name, value)
    razorpay = FakeRazorpay(now=datetime.now(UTC))
    sheets = FakeSheets(TEST_SPREADSHEET_ID)
    with respx.mock(assert_all_called=False) as router:
        razorpay.mount(router)
        sheets.mount(router)
        config_path = write_serve_config(
            tmp_path / config_dir, free_port(), account=RECORDED_ACCOUNT, api_url=RAZORPAY_API
        )
        with start(config_path):
            pass
        asyncio.run(_first_flush(config_path))
        yield Setup(config_path, razorpay, sheets, router)


def doctor(on: Setup, capsys: pytest.CaptureFixture[str], *args: str) -> Report:
    capsys.readouterr()
    exit_code = main(["doctor", "--config", str(on.config_path), *args])
    lines: dict[str, tuple[str, str]] = {}
    for line in capsys.readouterr().out.splitlines():
        state, _, rest = line.partition(" ")
        check, _, says = rest.strip().partition(": ")
        lines[check] = (state, says)
    return Report(exit_code, lines)


def test_a_healthy_setup_prints_one_ok_line_per_check_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with healthy(tmp_path, monkeypatch) as on:
        report = doctor(on, capsys)

    assert list(report.lines) == CHECKS
    assert {state for state, _ in report.lines.values()} == {"ok"}
    assert report.exit_code == 0
    assert "service account" in report.says("sheet")


# -- each check, broken on purpose (AC-60-2) -----------------------------------------------------

type Break = Callable[[Setup, pytest.MonkeyPatch], None]


def config_typo(on: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    text = on.config_path.read_text(encoding="utf-8")
    on.config_path.write_text(text.replace("timezone =", "timezon ="), encoding="utf-8")


def missing_secret(on: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAZORPAY_WEBHOOK_SECRET")


def live_key_with_test_mode(on: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAZORPAY_KEY_ID", LIVE_KEY_ID)


def clock_off(on: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    on.razorpay.now -= timedelta(seconds=95)


def unshared_sheet(on: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    on.sheets.access = "none"


def moved_header(on: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    tab = on.sheets.tab(PAYMENTS)
    first, second = tab.value(0, 2), tab.value(0, 3)
    on.sheets.apply(
        [edit_cell(tab.sheet_id, 0, 2, str(second)), edit_cell(tab.sheet_id, 0, 3, str(first))]
    )


def read_only_database(on: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    os.chmod(on.database, stat.S_IREAD)


def pending_migration(on: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    on.sql("DELETE FROM schema_version WHERE version = (SELECT max(version) FROM schema_version)")


def nothing(on: Setup, monkeypatch: pytest.MonkeyPatch) -> None:
    pass


@pytest.mark.parametrize(
    ("broken", "check", "reason", "config_dir"),
    [
        (config_typo, "config", "unknown key service.timezon", "conf"),
        (missing_secret, "secrets", "RAZORPAY_WEBHOOK_SECRET is missing or empty", "conf"),
        (live_key_with_test_mode, "razorpay key", "a test-mode key starts rzp_test_", "conf"),
        (clock_off, "clock", "s ahead of Razorpay's, more than 30 s", "conf"),
        (unshared_sheet, "sheet", "the spreadsheet is not shared with the service account", "conf"),
        (moved_header, "sheet", "the Payments tab's header differs", "conf"),
        (read_only_database, "database", "read-only", "conf"),
        (pending_migration, "database", "1 migration(s) pending", "conf"),
        (nothing, "database", "in a synced folder (OneDrive - Fixture)", "OneDrive - Fixture/conf"),
    ],
    ids=[
        "config-typo",
        "missing-secret",
        "live-key-with-test-mode",
        "clock-off",
        "unshared-sheet",
        "moved-header",
        "read-only-database",
        "pending-migration",
        "synced-folder",
    ],
)
def test_a_check_broken_on_purpose_fails_with_its_reason_and_the_rest_still_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    broken: Break,
    check: str,
    reason: str,
    config_dir: str,
) -> None:
    if broken is read_only_database and getattr(os, "geteuid", lambda: 1)() == 0:
        pytest.skip("root writes a read-only file")
    with healthy(tmp_path, monkeypatch, config_dir=config_dir) as on:
        broken(on, monkeypatch)
        try:
            report = doctor(on, capsys)
        finally:
            os.chmod(on.database, stat.S_IREAD | stat.S_IWRITE)

    assert list(report.lines) == CHECKS
    assert report.state(check) == "fail"
    assert reason in report.says(check)
    assert report.exit_code == 1
    if broken is not config_typo:  # a config that doesn't load leaves other checks unable to run
        assert report.others_ok(check)


def test_a_config_typo_still_lets_the_secrets_and_the_database_be_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with healthy(tmp_path, monkeypatch) as on:
        config_typo(on, monkeypatch)
        report = doctor(on, capsys)

    assert (report.state("secrets"), report.state("database")) == ("ok", "ok")
    assert report.says("sheet") == "not checked: the config file must load first"


def test_two_broken_checks_are_both_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with healthy(tmp_path, monkeypatch) as on:
        unshared_sheet(on, monkeypatch)
        pending_migration(on, monkeypatch)
        report = doctor(on, capsys)

    assert (report.state("sheet"), report.state("database")) == ("fail", "fail")
    assert report.others_ok("sheet", "database")


def test_a_key_razorpay_refuses_fails_the_key_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with healthy(tmp_path, monkeypatch) as on:
        on.razorpay.key_refused = True
        report = doctor(on, capsys)

    assert report.state("razorpay key") == "fail"
    assert "Razorpay refused the key (HTTP 401)" in report.says("razorpay key")


def test_before_the_first_serve_the_missing_database_is_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with healthy(tmp_path, monkeypatch) as on:
        on.database.unlink()
        report = doctor(on, capsys)

    assert report.state("database") == "warning"
    assert "wsync serve makes it" in report.says("database")
    assert report.state("sheet") == "ok"  # its tabs found by title, their headers compared
    assert report.exit_code == 0


def test_a_database_not_in_wal_mode_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with healthy(tmp_path, monkeypatch) as on:
        on.sql("PRAGMA journal_mode=DELETE")
        report = doctor(on, capsys)

    assert report.state("database") == "fail"
    assert "its journal mode is delete, not WAL" in report.says("database")


@pytest.mark.parametrize(
    ("kind", "found"),
    [("nfs4", "nfs4"), ("cifs", "cifs"), ("fuse.sshfs", "fuse.sshfs"), ("ext4", None)],
)
def test_a_data_directory_on_a_network_mount_is_told_from_a_local_one(
    kind: str, found: str | None
) -> None:
    mounts = f"/dev/sda1 / ext4 rw 0 0\nserver:/export /srv/shared\\040data {kind} rw 0 0\n"

    assert network_filesystem(PurePosixPath("/srv/shared data/wsync/data"), mounts) == found
    assert network_filesystem(PurePosixPath("/srv/elsewhere/data"), mounts) is None


# -- Razorpay's webhook: only ever a warning (AC-60-3) --------------------------------------------


@pytest.mark.parametrize(
    ("webhooks", "status", "reason"),
    [
        ([], 200, "no webhook is set up for this key's mode"),
        ([listed_webhook(active=False, disabled_at=1790000500)], 200, "the webhook is disabled"),
        (
            [listed_webhook(events={"payment.captured": True, "refund.created": False})],
            200,
            "lacks a handled event: payment.authorized, payment.failed, refund.created",
        ),
        ([listed_webhook(secret_exists=False)], 200, "has no secret"),
        ([listed_webhook()], 404, "can't check: Razorpay answered HTTP 404"),
    ],
    ids=["missing", "disabled", "lacks-an-event", "no-secret", "list-refused"],
)
def test_a_webhook_that_is_not_right_is_a_warning_and_the_exit_code_stays_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    webhooks: list[dict[str, Any]],
    status: int,
    reason: str,
) -> None:
    with healthy(tmp_path, monkeypatch) as on:
        on.razorpay.webhooks = webhooks
        on.razorpay.webhooks_status = status
        report = doctor(on, capsys)

    assert report.state("razorpay webhook") == "warning"
    assert reason in report.says("razorpay webhook")
    assert report.others_ok("razorpay webhook")
    assert report.exit_code == 0


def test_with_url_the_webhook_looked_for_is_the_one_posting_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    elsewhere = "https://other.example/webhooks/razorpay"
    with healthy(tmp_path, monkeypatch) as on:
        rejected = httpx.Response(400, json={"rejection": "bad_signature"})
        on.router.post(elsewhere).mock(return_value=rejected)
        on.router.post(WEBHOOK_URL).mock(return_value=rejected)
        ours = doctor(on, capsys, "--url", WEBHOOK_URL)
        other = doctor(on, capsys, "--url", elsewhere)

    assert ours.state("razorpay webhook") == "ok"
    assert other.state("razorpay webhook") == "warning"
    assert f"no webhook posts to {elsewhere}" in other.says("razorpay webhook")


# -- the public URL (AC-60-4) ---------------------------------------------------------------------


NO_SUCH_HOST = "https://wsync-doctor.invalid/webhooks/razorpay"
"""A name no resolver knows: `.invalid` is reserved for that (RFC 2606)."""


def requests_of(calls: object) -> list[httpx.Request]:
    return [call.request for call in cast(list[respx.models.Call], calls)]


@pytest.mark.parametrize(
    ("answer", "state", "reason"),
    [
        (
            httpx.Response(400, json={"rejection": "bad_signature"}),
            "ok",
            "the receiver answered its own 400 (bad_signature)",
        ),
        (
            httpx.Response(200, html="<!DOCTYPE html><html><body>Visit site?</body></html>"),
            "fail",
            "answered HTTP 200 with an HTML page, not the receiver",
        ),
        (
            httpx.Response(301, headers={"location": "https://elsewhere.example/"}),
            "fail",
            "answered a redirect (HTTP 301)",
        ),
        (httpx.ReadTimeout("timed out"), "fail", "no answer within 10 s"),
        (None, "fail", "the name wsync-doctor.invalid does not resolve (DNS)"),
        (httpx.Response(404, json={"detail": "Not Found"}), "fail", "not the receiver's 400"),
    ],
    ids=["our-400", "html-200", "redirect", "timeout", "dns", "another-status"],
)
def test_the_public_url_passes_only_on_the_receivers_own_400(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: httpx.Response | Exception | None,
    state: str,
    reason: str,
) -> None:
    url = WEBHOOK_URL if answer is not None else NO_SUCH_HOST
    with healthy(tmp_path, monkeypatch) as on:
        route = on.router.post(url)
        if isinstance(answer, httpx.Response):
            route.mock(return_value=answer)
        elif answer is None:
            route.pass_through()  # the real lookup, which fails with or without a network
        else:
            route.mock(side_effect=answer)
        on.razorpay.webhooks = [listed_webhook(url)]
        report = doctor(on, capsys, "--url", url)
        sent = [request.content for request in requests_of(route.calls)]

    assert list(report.lines) == [*CHECKS, "public url"]
    assert report.state("public url") == state
    assert reason in report.says("public url")
    assert report.exit_code == (0 if state == "ok" else 1)
    assert report.others_ok("public url")
    assert set(sent) == {b""}  # an empty body, every time it was asked


# -- doctor writes nothing (AC-60-5) --------------------------------------------------------------


def test_doctor_changes_nothing_in_the_database_the_sheet_or_razorpay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def state(on: Setup) -> tuple[list[str], int, list[list[list[Any]]]]:
        with closing(sqlite3.connect(on.database)) as db:
            return list(db.iterdump()), len(on.sheets.written), [t.values() for t in on.sheets.tabs]

    with healthy(tmp_path, monkeypatch) as on:
        on.router.post(WEBHOOK_URL).mock(
            return_value=httpx.Response(400, json={"rejection": "bad_signature"})
        )
        before, asked = state(on), len(on.router.calls)
        report = doctor(on, capsys, "--url", WEBHOOK_URL)
        requests = requests_of(on.router.calls)[asked:]
        after = state(on)

    assert report.exit_code == 0
    assert after == before
    razorpay = [r for r in requests if str(r.url).startswith(RAZORPAY_API)]
    assert {r.method for r in razorpay} == {"GET"}
    posted = [str(r.url) for r in requests if r.method == "POST"]
    assert all(":getByDataFilter" in url or url in (WEBHOOK_URL, TOKEN_URL) for url in posted)
