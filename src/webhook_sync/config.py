"""Configuration: `webhook-sync.toml` plus secrets from the environment or `.env`, failing closed.

Non-secret settings live in the TOML; secrets come from environment variables, or a `.env` file
next to the config file, the real environment winning (A11). Paths in the TOML are relative to the
config file (A10). Anything unsafe raises ConfigError with one line naming the key or variable,
never a secret's value: an unknown key, a secret in the TOML, a missing or empty secret, a key
whose prefix doesn't match the mode, a bad timezone, `[sources.stripe]` while Stripe is parked.
The sections P2-P4 own are accepted and checked for type only (A29).
"""

import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import dotenv_values

type Mode = Literal["test", "live"]
type RetryProfile = Literal["production", "demo"]

MODES: tuple[Mode, ...] = ("test", "live")
RETRY_PROFILES: tuple[RetryProfile, ...] = ("production", "demo")

DEFAULT_PUBLIC_LISTEN = "0.0.0.0:8080"
DEFAULT_ADMIN_LISTEN = "127.0.0.1:8081"
DEFAULT_DATA_DIR = "data"
DEFAULT_TIMEZONE = "Asia/Kolkata"  # Q20
DEFAULT_RAZORPAY_API_URL = "https://api.razorpay.com"


class ConfigError(Exception):
    """The configuration is unusable; the message names the key or variable, never a value."""


@dataclass(frozen=True)
class ServiceConfig:
    public_listen: str
    data_dir: Path
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo(DEFAULT_TIMEZONE))
    admin_listen: str = DEFAULT_ADMIN_LISTEN
    retry_profile: RetryProfile = "production"


@dataclass(frozen=True)
class RazorpayConfig:
    mode: Mode
    account: str
    """Always `acc_<Merchant ID>`, however the TOML wrote it (A7)."""
    metadata_keys: tuple[str, ...] = ()
    api_url: str = DEFAULT_RAZORPAY_API_URL
    """Where the fetch calls Razorpay's API. Only tests and fakes point it elsewhere."""


@dataclass(frozen=True)
class SheetsConfig:
    spreadsheet_id: str


@dataclass(frozen=True)
class DemoConfig:
    spreadsheet_ids: tuple[str, ...] = ()


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
    sheets: SheetsConfig
    sinks: tuple[str, ...]
    """The configured sinks' names, one per `[sinks.*]` section present (today `sheets`). Each
    real ledger change makes one mirror job per sink (A19)."""
    demo: DemoConfig = DemoConfig()


SECRET_NAMES = ("RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET")

# --- The keys the TOML may hold, each with its type. A nested dict is a table. ---

type Check = Callable[[Any], bool]


def _is_string(value: Any) -> bool:
    return isinstance(value, str)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in cast(list[Any], value))


_TYPE_NAMES: dict[Check, str] = {
    _is_string: "a string",
    _is_count: "a whole number, 0 or more",
    _is_bool: "true or false",
    _is_string_list: "a list of strings",
}

type Schema = Mapping[str, "Schema | Check"]

SCHEMA: Schema = {
    "service": {
        "public_listen": _is_string,
        "admin_listen": _is_string,
        "data_dir": _is_string,
        "timezone": _is_string,
        "retry_profile": _is_string,
    },
    "sources": {
        "razorpay": {
            "mode": _is_string,
            "account": _is_string,
            "metadata_keys": _is_string_list,
            "api_url": _is_string,
        },
    },
    "sinks": {
        "sheets": {
            "spreadsheet_id": _is_string,
            "columns": _is_string_list,
            "mask_email": _is_bool,
        },
    },
    "sweeper": {"initial_lookback_days": _is_count, "heartbeat_url": _is_string},
    "verify": {"days": _is_count, "nightly_at": _is_string, "auto_repair": _is_bool},
    "retention": {"payload_days": _is_count},
    "alerts": {"url": _is_string},
    "demo": {"spreadsheet_ids": _is_string_list},
}

PARKED = {"sources.stripe": "Stripe is parked (Q103); remove [sources.stripe]"}

_SECRET_WORDS = ("secret", "token", "password", "credential", "key_id", "api_key", "private_key")


def locate_data_dir(path: Path) -> Path:
    """The data directory `[service] data_dir` names, relative to the config file (A10)."""
    service = _read_toml(path).get("service", {})
    data_dir: Any = (
        cast(Mapping[str, Any], service).get("data_dir", DEFAULT_DATA_DIR)
        if isinstance(service, dict)
        else None
    )
    if not isinstance(data_dir, str) or not data_dir:
        raise ConfigError("service.data_dir must be a non-empty string")
    return (path.parent / data_dir).resolve()


def load_config(path: Path, environ: Mapping[str, str] | None = None) -> Config:
    """Read the config file at `path` and the secrets; raise ConfigError naming what is wrong."""
    document = _read_toml(path)
    _check_keys(document, SCHEMA, prefix="")
    secrets = _secrets(path.parent / ".env", os.environ if environ is None else environ)
    _refuse_secret_values(document, secrets, prefix="")

    service = _section(document, "service")
    razorpay = _section(_section(document, "sources"), "razorpay", "sources.", required=True)
    sheets = _section(_section(document, "sinks"), "sheets", "sinks.", required=True)
    demo = _section(document, "demo")

    mode: Mode = _choice(razorpay, "mode", "sources.razorpay", MODES)
    key_prefix = f"rzp_{mode}_"
    if not secrets["RAZORPAY_KEY_ID"].startswith(key_prefix):
        raise ConfigError(
            f'RAZORPAY_KEY_ID does not match sources.razorpay.mode = "{mode}": '
            f"a {mode}-mode key starts {key_prefix}"
        )
    webhook_secrets = tuple(
        s.strip() for s in secrets["RAZORPAY_WEBHOOK_SECRET"].split(",") if s.strip()
    )
    if not webhook_secrets:
        raise ConfigError("RAZORPAY_WEBHOOK_SECRET is missing or empty")

    return Config(
        service=ServiceConfig(
            public_listen=_listen(service, "public_listen", DEFAULT_PUBLIC_LISTEN),
            admin_listen=_listen(service, "admin_listen", DEFAULT_ADMIN_LISTEN),
            data_dir=locate_data_dir(path),
            timezone=_timezone(service),
            retry_profile=_choice(
                service, "retry_profile", "service", RETRY_PROFILES, "production"
            ),
        ),
        razorpay=RazorpayConfig(
            mode=mode,
            account=_account(razorpay),
            metadata_keys=tuple(razorpay.get("metadata_keys", [])),
            api_url=_api_url(razorpay),
        ),
        razorpay_secrets=RazorpaySecrets(
            key_id=secrets["RAZORPAY_KEY_ID"],
            key_secret=secrets["RAZORPAY_KEY_SECRET"],
            webhook_secrets=webhook_secrets,
        ),
        sheets=SheetsConfig(spreadsheet_id=_string(sheets, "spreadsheet_id", "sinks.sheets")),
        sinks=tuple(_section(document, "sinks")),
        demo=DemoConfig(spreadsheet_ids=tuple(demo.get("spreadsheet_ids", []))),
    )


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"config file {path} is not UTF-8 text") from None
    except OSError as error:
        raise ConfigError(f"config file {path} cannot be read: {error.strerror}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path} is not valid TOML: {error}") from None


def _check_keys(table: Mapping[str, Any], schema: Schema, prefix: str) -> None:
    """Refuse a secret-looking key, a parked or unknown key, or a value of the wrong type."""
    for key, value in table.items():
        name = f"{prefix}{key}"
        if any(word in key.lower() for word in _SECRET_WORDS):
            raise ConfigError(
                f"{name} looks like a secret: secrets belong in the environment or .env, "
                "never in the config file"
            )
        if name in PARKED:
            raise ConfigError(f"[{name}] is refused: {PARKED[name]}")
        expected = schema.get(key)
        if expected is None:
            raise ConfigError(f"unknown key {name}")
        if isinstance(expected, Mapping):
            if not isinstance(value, dict):
                raise ConfigError(f"{name} must be a table, [{name}]")
            _check_keys(cast(Mapping[str, Any], value), cast(Schema, expected), f"{name}.")
        elif not expected(value):
            raise ConfigError(f"{name} must be {_TYPE_NAMES[expected]}")


def _refuse_secret_values(
    table: Mapping[str, Any], secrets: Mapping[str, str], prefix: str
) -> None:
    """Refuse the TOML if any string in it carries a secret's value (A11), naming the key."""
    values = [
        part.strip()
        for value in secrets.values()
        for part in value.split(",")
        if len(part.strip()) >= 8
    ]
    for key, value in table.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            _refuse_secret_values(cast(Mapping[str, Any], value), secrets, f"{name}.")
            continue
        strings = cast(list[Any], value) if isinstance(value, list) else [value]
        for item in strings:
            if isinstance(item, str) and any(secret in item for secret in values):
                raise ConfigError(
                    f"{name} holds a secret's value: secrets belong in the environment or .env, "
                    "never in the config file"
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
        secrets[name] = value.strip()
    return secrets


def _section(
    document: Mapping[str, Any], key: str, prefix: str = "", required: bool = False
) -> Mapping[str, Any]:
    value = document.get(key)
    if value is None and not required:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{prefix}{key}] is missing")
    return cast(Mapping[str, Any], value)


def _string(table: Mapping[str, Any], key: str, section: str, default: str | None = None) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{section}.{key} must be a non-empty string")
    return value


def _choice[T: str](
    table: Mapping[str, Any],
    key: str,
    section: str,
    choices: tuple[T, ...],
    default: T | None = None,
) -> T:
    value = table.get(key, default)
    for choice in choices:
        if value == choice:
            return choice
    allowed = " or ".join(f'"{c}"' for c in choices)
    raise ConfigError(f"{section}.{key} must be {allowed}")


def _listen(service: Mapping[str, Any], key: str, default: str) -> str:
    listen = _string(service, key, "service", default)
    host, _, port = listen.rpartition(":")
    if not host.strip("[]") or not port.isdigit() or not 0 < int(port) < 65536:
        raise ConfigError(f'service.{key} must be "host:port", with a port from 1 to 65535')
    return listen


def _timezone(service: Mapping[str, Any]) -> ZoneInfo:
    name = _string(service, "timezone", "service", default=DEFAULT_TIMEZONE)
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        raise ConfigError(
            "service.timezone is not a known time zone name (e.g. Asia/Kolkata)"
        ) from None


def _api_url(razorpay: Mapping[str, Any]) -> str:
    url = _string(razorpay, "api_url", "sources.razorpay", DEFAULT_RAZORPAY_API_URL).rstrip("/")
    if not re.fullmatch(r"https?://[^/\s]+", url):
        raise ConfigError('sources.razorpay.api_url must be "https://host[:port]"')
    return url


_ACCOUNT = re.compile(r"(?:acc_)?([A-Za-z0-9]+)")


def _account(razorpay: Mapping[str, Any]) -> str:
    """`acc_X` or the bare Merchant ID `X`, stored as `acc_X` (A7)."""
    match = _ACCOUNT.fullmatch(_string(razorpay, "account", "sources.razorpay").strip())
    if match is None:
        raise ConfigError(
            "sources.razorpay.account must be acc_<Merchant ID> or the bare Merchant ID"
        )
    return f"acc_{match[1]}"
