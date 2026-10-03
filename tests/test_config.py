"""Loading `webhook-sync.toml` and the secrets (in process, through the function `serve` calls)."""

from pathlib import Path

import pytest
from support import TEST_ACCOUNT, TEST_SPREADSHEET_ID, fixture_service_account_b64

from webhook_sync.config import ConfigError, load_config

MINIMAL_TOML = f"""
[service]
public_listen = "127.0.0.1:9090"
data_dir = "state"
timezone = "Asia/Kolkata"

[sources.razorpay]
mode = "test"
account = "{TEST_ACCOUNT}"

[sinks.sheets]
spreadsheet_id = "{TEST_SPREADSHEET_ID}"
"""

DOTENV = f"""
RAZORPAY_KEY_ID=rzp_test_fromDotenv
RAZORPAY_KEY_SECRET=secret-from-dotenv
RAZORPAY_WEBHOOK_SECRET=webhook-secret-from-dotenv
GOOGLE_SERVICE_ACCOUNT_B64={fixture_service_account_b64()}
"""


def write_config(directory: Path, dotenv: str | None = None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "webhook-sync.toml"
    path.write_text(MINIMAL_TOML)
    if dotenv is not None:
        (directory / ".env").write_text(dotenv)
    return path


def test_the_data_dir_is_relative_to_the_config_file(tmp_path: Path) -> None:
    path = write_config(tmp_path / "conf", DOTENV)

    config = load_config(path, environ={})

    assert config.service.data_dir == (tmp_path / "conf" / "state").resolve()
    assert config.service.public_listen == "127.0.0.1:9090"
    assert (config.razorpay.mode, config.razorpay.account) == ("test", TEST_ACCOUNT)


def test_secrets_come_from_dotenv_and_the_real_environment_wins(tmp_path: Path) -> None:
    path = write_config(tmp_path, DOTENV)

    config = load_config(
        path, environ={"RAZORPAY_WEBHOOK_SECRET": "newest-from-env, older-from-env"}
    )

    secrets = config.razorpay_secrets
    assert secrets.key_id == "rzp_test_fromDotenv"
    assert secrets.key_secret == "secret-from-dotenv"
    assert secrets.webhook_secrets == ("newest-from-env", "older-from-env")


def test_the_configured_sinks_are_the_sinks_sections_present(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, DOTENV), environ={})

    assert config.sinks == ("sheets",)


def test_secrets_never_appear_in_the_configs_repr(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, DOTENV), environ={})

    assert "dotenv" not in repr(config)


def test_the_fetch_calls_razorpays_api_unless_api_url_points_elsewhere(tmp_path: Path) -> None:
    default = load_config(write_config(tmp_path / "default", DOTENV), environ={})
    path = write_config(tmp_path / "faked", DOTENV)
    path.write_text(
        MINIMAL_TOML.replace("[sinks.sheets]", 'api_url = "http://127.0.0.1:9/"\n\n[sinks.sheets]')
    )

    faked = load_config(path, environ={})

    assert default.razorpay.api_url == "https://api.razorpay.com"
    assert faked.razorpay.api_url == "http://127.0.0.1:9"


def test_an_api_url_that_is_not_an_http_address_refuses_to_start(tmp_path: Path) -> None:
    path = write_config(tmp_path, DOTENV)
    path.write_text(
        MINIMAL_TOML.replace("[sinks.sheets]", 'api_url = "api.razorpay.com"\n\n[sinks.sheets]')
    )

    with pytest.raises(ConfigError, match=r"sources\.razorpay\.api_url"):
        load_config(path, environ={})
