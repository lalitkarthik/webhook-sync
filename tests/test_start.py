"""Fail-closed start, in process (seam 3's fast cases).

Each case goes through `start`, the startup function `wsync serve` calls, and asserts on the one
line a refusal prints: it names the key or variable, and never a secret's value.
"""

from pathlib import Path

import pytest
from support import (
    TEST_ACCOUNT,
    TEST_KEY_ID,
    TEST_KEY_SECRET,
    TEST_SPREADSHEET_ID,
    TEST_WEBHOOK_SECRET,
    fixture_secrets,
)

from webhook_sync.cli.serve import StartRefused, start

VALID_TOML = f"""
[service]
data_dir = "data"
timezone = "Asia/Kolkata"

[sources.razorpay]
mode = "test"
account = "{TEST_ACCOUNT}"

[sinks.sheets]
spreadsheet_id = "{TEST_SPREADSHEET_ID}"
"""

SECRET_VALUES = (TEST_KEY_ID, TEST_KEY_SECRET, TEST_WEBHOOK_SECRET)


def write_toml(directory: Path, text: str) -> Path:
    path = directory / "webhook-sync.toml"
    path.write_text(text, encoding="utf-8")
    return path


def merge_toml(base: str, extra: str) -> str:
    """`extra`'s lines added to `base`: a table `extra` opens is merged with base's table."""
    lines = base.strip().splitlines()
    current_table = ""
    for line in extra.strip().splitlines():
        if line.startswith("["):
            current_table = line
            if line not in lines:
                lines += ["", line]
            continue
        if current_table:
            lines.insert(lines.index(current_table) + 1, line)
        else:
            lines.insert(0, line)
    return "\n".join(lines) + "\n"


def refusal(path: Path, environ: dict[str, str] | None = None) -> str:
    """The line `wsync serve` would print for this config and environment."""
    with (
        pytest.raises(StartRefused) as refused,
        start(path, fixture_secrets() if environ is None else environ),
    ):
        pass
    line = str(refused.value)
    assert "\n" not in line
    assert [value for value in SECRET_VALUES if value in line] == []
    return line


@pytest.mark.parametrize(
    ("extra", "named"),
    [
        ("[service]\nport = 1\n", "service.port"),
        ("[sources.razorpay]\nmodee = 'test'\n", "sources.razorpay.modee"),
        ("[sinks.sheets]\ncolumnz = []\n", "sinks.sheets.columnz"),
        ("[sweeper]\nlookback = 3\n", "sweeper.lookback"),
        ("[sources.paypal]\nmode = 'test'\n", "sources.paypal"),
        ("[sinks.excel]\npath = 'x'\n", "sinks.excel"),
        ("[admin]\nport = 1\n", "admin"),
        ("verbose = true\n", "verbose"),
    ],
)
def test_an_unknown_key_is_refused_by_name(tmp_path: Path, extra: str, named: str) -> None:
    document = merge_toml(VALID_TOML, extra)

    assert named in refusal(write_toml(tmp_path, document))


@pytest.mark.parametrize(
    "name", ["RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"]
)
@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_missing_or_empty_secret_is_refused_by_name(
    tmp_path: Path, name: str, value: str | None
) -> None:
    environ = fixture_secrets()
    if value is None:
        del environ[name]
    else:
        environ[name] = value

    assert name in refusal(write_toml(tmp_path, VALID_TOML), environ)


def test_a_webhook_secret_of_only_commas_is_refused(tmp_path: Path) -> None:
    environ = fixture_secrets() | {"RAZORPAY_WEBHOOK_SECRET": " , ,"}

    assert "RAZORPAY_WEBHOOK_SECRET" in refusal(write_toml(tmp_path, VALID_TOML), environ)


@pytest.mark.parametrize(
    ("mode", "key_id"),
    [("live", TEST_KEY_ID), ("test", "rzp_live_fixtureKeyId01"), ("test", "fixtureKeyId01")],
)
def test_a_key_id_whose_prefix_does_not_match_the_mode_is_refused(
    tmp_path: Path, mode: str, key_id: str
) -> None:
    document = VALID_TOML.replace('mode = "test"', f'mode = "{mode}"')
    environ = fixture_secrets() | {"RAZORPAY_KEY_ID": key_id}

    line = refusal(write_toml(tmp_path, document), environ)

    assert "RAZORPAY_KEY_ID" in line
    assert "sources.razorpay.mode" in line
    assert key_id not in line


@pytest.mark.parametrize(
    ("extra", "named"),
    [
        ("[sources.razorpay]\nwebhook_secret = 'anything'\n", "sources.razorpay.webhook_secret"),
        ("RAZORPAY_KEY_SECRET = 'anything'\n", "RAZORPAY_KEY_SECRET"),
        (f"[alerts]\nurl = 'https://hooks.example/{TEST_KEY_SECRET}'\n", "alerts.url"),
        (f"[demo]\nspreadsheet_ids = ['{TEST_WEBHOOK_SECRET}']\n", "demo.spreadsheet_ids"),
    ],
)
def test_a_secret_in_the_toml_is_refused_by_key(tmp_path: Path, extra: str, named: str) -> None:
    line = refusal(write_toml(tmp_path, merge_toml(VALID_TOML, extra)))

    assert named in line
    assert "secret" in line


@pytest.mark.parametrize("timezone", ["Asia/Kolkatta", "../../etc/passwd", ""])
def test_a_bad_timezone_is_refused(tmp_path: Path, timezone: str) -> None:
    document = VALID_TOML.replace('"Asia/Kolkata"', f'"{timezone}"')

    assert "service.timezone" in refusal(write_toml(tmp_path, document))


def test_the_timezone_defaults_to_asia_kolkata(tmp_path: Path) -> None:
    document = VALID_TOML.replace('timezone = "Asia/Kolkata"', "")

    with start(write_toml(tmp_path, document), fixture_secrets()) as config:
        assert config.service.timezone.key == "Asia/Kolkata"


def test_sources_stripe_is_refused_while_stripe_is_parked(tmp_path: Path) -> None:
    extra = "[sources.stripe]\nmode = 'test'\n"

    line = refusal(write_toml(tmp_path, merge_toml(VALID_TOML, extra)))

    assert "sources.stripe" in line
    assert "parked" in line


@pytest.mark.parametrize(
    ("document", "named"),
    [
        (VALID_TOML.replace(f'spreadsheet_id = "{TEST_SPREADSHEET_ID}"', ""), "sinks.sheets"),
        (VALID_TOML.replace(f'"{TEST_SPREADSHEET_ID}"', '""'), "sinks.sheets.spreadsheet_id"),
        (VALID_TOML.split("[sinks.sheets]")[0], "sinks.sheets"),
        (VALID_TOML.replace(f'account = "{TEST_ACCOUNT}"', ""), "sources.razorpay.account"),
        (VALID_TOML.replace(f'"{TEST_ACCOUNT}"', '"acc-Not_Valid"'), "sources.razorpay.account"),
        (VALID_TOML.replace('mode = "test"', 'mode = "sandbox"'), "sources.razorpay.mode"),
        (VALID_TOML + "[verify]\ndays = '30'\n", "verify.days"),
        (VALID_TOML + "[sweeper]\ninitial_lookback_days = -1\n", "sweeper.initial_lookback_days"),
        (VALID_TOML + "[demo]\nspreadsheet_ids = 'one'\n", "demo.spreadsheet_ids"),
        (merge_toml(VALID_TOML, "[service]\nretry_profile = 'fast'"), "service.retry_profile"),
        (merge_toml(VALID_TOML, "[service]\npublic_listen = 'nohost'"), "service.public_listen"),
        (merge_toml(VALID_TOML, "[service]\nadmin_listen = ':0'"), "service.admin_listen"),
    ],
)
def test_a_missing_or_malformed_setting_is_refused_by_name(
    tmp_path: Path, document: str, named: str
) -> None:
    assert named in refusal(write_toml(tmp_path, document))


@pytest.mark.parametrize("account", [TEST_ACCOUNT, TEST_ACCOUNT.removeprefix("acc_")])
def test_the_account_is_accepted_bare_or_prefixed_and_kept_as_acc(
    tmp_path: Path, account: str
) -> None:
    document = VALID_TOML.replace(f'"{TEST_ACCOUNT}"', f'"{account}"')

    with start(write_toml(tmp_path, document), fixture_secrets()) as config:
        assert config.razorpay.account == TEST_ACCOUNT


def test_defaults_are_ports_8080_and_8081_and_a_data_dir_beside_the_config(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "conf"
    config_dir.mkdir()

    with start(write_toml(config_dir, VALID_TOML), fixture_secrets()) as config:
        assert config.service.public_listen == "0.0.0.0:8080"
        assert config.service.admin_listen == "127.0.0.1:8081"
        assert config.service.data_dir == (config_dir / "data").resolve()
        assert config.service.retry_profile == "production"
        assert (config_dir / "data" / "webhook-sync.db").is_file()


def test_the_example_config_validates(tmp_path: Path) -> None:
    example = Path(__file__).parents[1] / "webhook-sync.toml.example"
    path = write_toml(tmp_path, example.read_text(encoding="utf-8"))

    with start(path, fixture_secrets()) as config:
        assert config.razorpay.mode == "test"
        assert config.service.timezone.key == "Asia/Kolkata"
