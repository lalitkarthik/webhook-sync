"""P3's settings at start, in process (A2, A4, A5, A19): the service-account key, the Payments
columns, `mask_email`, `[alerts] url` and a loopback-only `admin_listen`.

Each refusal names the key or variable and never a secret's value (`refusal` checks it).
"""

from pathlib import Path

import pytest
from support import fixture_secrets, fixture_service_account, fixture_service_account_b64
from test_start import VALID_TOML, merge_toml, refusal, write_toml

from webhook_sync.cli.serve import start
from webhook_sync.config import DEFAULT_COLUMNS

KEY = "GOOGLE_SERVICE_ACCOUNT_B64"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_missing_or_empty_service_account_key_is_refused_by_name(
    tmp_path: Path, value: str | None
) -> None:
    environ = fixture_secrets()
    if value is None:
        del environ[KEY]
    else:
        environ[KEY] = value

    assert KEY in refusal(write_toml(tmp_path, VALID_TOML), environ)


@pytest.mark.parametrize(
    "value",
    [
        "not base64 at all!",
        "bm90IGpzb24=",  # "not json"
        fixture_service_account_b64(type="authorized_user"),
        fixture_service_account_b64(client_email=None),
        fixture_service_account_b64(private_key="-----BEGIN PRIVATE KEY-----\nnope\n"),
    ],
    ids=["not-base64", "not-json", "not-a-service-account", "no-client-email", "bad-private-key"],
)
def test_a_value_that_is_not_a_service_account_key_is_refused_naming_only_the_variable(
    tmp_path: Path, value: str
) -> None:
    line = refusal(write_toml(tmp_path, VALID_TOML), fixture_secrets() | {KEY: value})

    assert KEY in line
    assert value not in line
    assert "fixture-project" not in line


def test_the_service_account_key_is_decoded_and_kept_out_of_the_repr(tmp_path: Path) -> None:
    with start(write_toml(tmp_path, VALID_TOML), fixture_secrets()) as config:
        assert config.google_secrets.service_account == fixture_service_account()
        assert "gserviceaccount" not in repr(config)
        assert "PRIVATE KEY" not in repr(config)


def test_the_columns_default_to_q136_and_emails_are_masked(tmp_path: Path) -> None:
    with start(write_toml(tmp_path, VALID_TOML), fixture_secrets()) as config:
        assert config.sheets.columns == DEFAULT_COLUMNS
        assert config.sheets.columns[:4] == ("payment_id", "source", "created", "status")
        assert config.sheets.mask_email is True
        assert config.alerts.url == ""


def test_columns_mask_email_and_the_alerts_url_are_read_from_the_toml(tmp_path: Path) -> None:
    document = merge_toml(
        VALID_TOML,
        '[sinks.sheets]\ncolumns = ["payment_id", "amount", "metadata", "note"]\n'
        "mask_email = false\n"
        '[alerts]\nurl = "https://hooks.slack.test/services/T0/B0/x"',
    )

    with start(write_toml(tmp_path, document), fixture_secrets()) as config:
        assert config.sheets.columns == ("payment_id", "amount", "metadata", "note")
        assert config.sheets.mask_email is False
        assert config.alerts.url == "https://hooks.slack.test/services/T0/B0/x"


@pytest.mark.parametrize(
    ("columns", "named"),
    [
        ('["payment_id", "settled"]', "settled"),
        ('["payment_id", "amount", "amount"]', "amount"),
        ('["amount"]', "payment_id"),
    ],
    ids=["unknown", "repeated", "no-payment-id"],
)
def test_a_bad_columns_list_is_refused_naming_the_column(
    tmp_path: Path, columns: str, named: str
) -> None:
    document = merge_toml(VALID_TOML, f"[sinks.sheets]\ncolumns = {columns}")

    line = refusal(write_toml(tmp_path, document))

    assert "sinks.sheets.columns" in line
    assert named in line


@pytest.mark.parametrize(
    "listen", ["0.0.0.0:8081", "192.168.1.5:8081", "example.com:8081", "[::]:8081"]
)
def test_an_admin_listen_that_is_not_loopback_is_refused(tmp_path: Path, listen: str) -> None:
    document = merge_toml(VALID_TOML, f'[service]\nadmin_listen = "{listen}"')

    line = refusal(write_toml(tmp_path, document))

    assert "service.admin_listen" in line
    assert "loopback" in line


@pytest.mark.parametrize("listen", ["127.0.0.1:9001", "localhost:9001", "[::1]:9001"])
def test_a_loopback_admin_listen_is_accepted(tmp_path: Path, listen: str) -> None:
    document = merge_toml(VALID_TOML, f'[service]\nadmin_listen = "{listen}"')

    with start(write_toml(tmp_path, document), fixture_secrets()) as config:
        assert config.service.admin_listen == listen


def test_an_alerts_url_that_is_not_an_http_address_is_refused(tmp_path: Path) -> None:
    document = merge_toml(VALID_TOML, '[alerts]\nurl = "hooks.slack.com/services/x"')

    assert "alerts.url" in refusal(write_toml(tmp_path, document))
