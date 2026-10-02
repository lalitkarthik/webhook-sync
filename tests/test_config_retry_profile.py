"""`[service] retry_profile` (A8): production by default, or demo; anything else refuses."""

from pathlib import Path

import pytest
from support import TEST_ACCOUNT, TEST_KEY_ID, TEST_KEY_SECRET, TEST_WEBHOOK_SECRET

from webhook_sync.config import ConfigError, load_config

SECRETS = {
    "RAZORPAY_KEY_ID": TEST_KEY_ID,
    "RAZORPAY_KEY_SECRET": TEST_KEY_SECRET,
    "RAZORPAY_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET,
}


def write_config(directory: Path, service_lines: str) -> Path:
    path = directory / "webhook-sync.toml"
    path.write_text(
        f'[service]\n{service_lines}\n[sources.razorpay]\nmode = "test"\naccount = "{TEST_ACCOUNT}"\n'
    )
    return path


@pytest.mark.parametrize(
    ("service_lines", "profile"),
    [("", "production"), ('retry_profile = "demo"', "demo")],
)
def test_the_retry_profile_is_production_unless_set(
    tmp_path: Path, service_lines: str, profile: str
) -> None:
    config = load_config(write_config(tmp_path, service_lines), environ=SECRETS)

    assert config.service.retry_profile == profile


def test_an_unknown_retry_profile_refuses_naming_the_key(tmp_path: Path) -> None:
    path = write_config(tmp_path, 'retry_profile = "fast"')

    with pytest.raises(ConfigError, match=r"service\.retry_profile"):
        load_config(path, environ=SECRETS)
