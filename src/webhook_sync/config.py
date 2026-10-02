"""Configuration: `webhook-sync.toml` plus secrets from the environment or `.env`.

Non-secret settings live in the TOML; secrets come from environment variables, or a `.env` file
next to the config file, the real environment winning (A11). Paths in the TOML are relative to the
config file (A10). This module reads the minimal P1 settings; full fail-closed validation is #9.
"""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from dotenv import dotenv_values

type Mode = Literal["test", "live"]
type RetryProfile = Literal["production", "demo"]


class ConfigError(Exception):
    """The configuration is unusable; the message names the key or variable, never a value."""


@dataclass(frozen=True)
class ServiceConfig:
    public_listen: str
    data_dir: Path
    retry_profile: RetryProfile = "production"


@dataclass(frozen=True)
class RazorpayConfig:
    mode: Mode
    account: str


@dataclass(frozen=True)
class RazorpaySecrets:
    """The Razorpay secrets. Values are kept out of `repr`, so they never reach a log."""

    key_id: str = field(repr=False)
    key_secret: str = field(repr=False)
    webhook_secrets: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True)
class Config:
    service: ServiceConfig
    razorpay: RazorpayConfig
    razorpay_secrets: RazorpaySecrets


SECRET_NAMES = ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET")


def load_config(path: Path, environ: Mapping[str, str] | None = None) -> Config:
    """Read the config file at `path` and the secrets; raise ConfigError naming what is wrong."""
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from None

    service = _table(document, "service")
    razorpay = _table(_table(document, "sources"), "razorpay", prefix="sources.")
    data_dir = Path(_string(service, "data_dir", "service", default="data"))
    mode = _string(razorpay, "mode", "sources.razorpay")
    if mode not in ("test", "live"):
        raise ConfigError("sources.razorpay.mode must be 'test' or 'live'")
    retry_profile = _string(service, "retry_profile", "service", default="production")
    if retry_profile not in ("production", "demo"):
        raise ConfigError("service.retry_profile must be 'production' or 'demo'")

    secrets = _secrets(path.parent / ".env", os.environ if environ is None else environ)
    return Config(
        service=ServiceConfig(
            public_listen=_string(service, "public_listen", "service", default="0.0.0.0:8080"),
            data_dir=(path.parent / data_dir).resolve(),
            retry_profile=retry_profile,
        ),
        razorpay=RazorpayConfig(
            mode=mode, account=_string(razorpay, "account", "sources.razorpay")
        ),
        razorpay_secrets=RazorpaySecrets(
            key_id=secrets["RAZORPAY_KEY_ID"],
            key_secret=secrets["RAZORPAY_KEY_SECRET"],
            webhook_secrets=tuple(
                s.strip() for s in secrets["RAZORPAY_WEBHOOK_SECRET"].split(",") if s.strip()
            ),
        ),
    )


def _secrets(dotenv_path: Path, environ: Mapping[str, str]) -> dict[str, str]:
    from_file = dotenv_values(dotenv_path) if dotenv_path.is_file() else {}
    merged: dict[str, str] = {k: v for k, v in from_file.items() if v is not None}
    merged.update(environ)
    secrets: dict[str, str] = {}
    for name in SECRET_NAMES:
        value = merged.get(name, "")
        if not value.strip():
            raise ConfigError(f"{name} is missing or empty")
        secrets[name] = value
    return secrets


def _table(document: Mapping[str, Any], key: str, prefix: str = "") -> Mapping[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"[{prefix}{key}] is missing")
    return cast(Mapping[str, Any], value)


def _string(table: Mapping[str, Any], key: str, section: str, default: str | None = None) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{section}.{key} must be a non-empty string")
    return value
