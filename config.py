from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class TelegramConfig:
    api_id: int
    api_hash: str
    phone_number: str
    twofa_password: str
    session_path: str


@dataclass(slots=True)
class ChannelsConfig:
    targets: list[str]


@dataclass(slots=True)
class NotifyConfig:
    serverchan_sendkey: str
    title_prefix: str
    max_text_len: int


@dataclass(slots=True)
class RuntimeConfig:
    log_level: str
    state_db_path: str
    backfill_limit: int
    dedup_window_days: int
    healthcheck_interval_sec: int


@dataclass(slots=True)
class AppConfig:
    telegram: TelegramConfig
    channels: ChannelsConfig
    notify: NotifyConfig
    runtime: RuntimeConfig


class ConfigError(ValueError):
    pass


def _require(data: dict[str, Any], key: str) -> Any:
    if key not in data:
        raise ConfigError(f"Missing config key: {key}")
    return data[key]


def load_config(path: str = "config.yaml") -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ConfigError("Top-level config must be a mapping")

    telegram = _require(raw, "telegram")
    channels = _require(raw, "channels")
    notify = _require(raw, "notify")
    runtime = _require(raw, "runtime")

    return AppConfig(
        telegram=TelegramConfig(
            api_id=int(_require(telegram, "api_id")),
            api_hash=str(_require(telegram, "api_hash")),
            phone_number=str(_require(telegram, "phone_number")),
            twofa_password=str(telegram.get("twofa_password", "")),
            session_path=str(_require(telegram, "session_path")),
        ),
        channels=ChannelsConfig(
            targets=[str(item) for item in _require(channels, "targets")],
        ),
        notify=NotifyConfig(
            serverchan_sendkey=str(_require(notify, "serverchan_sendkey")),
            title_prefix=str(_require(notify, "title_prefix")),
            max_text_len=int(_require(notify, "max_text_len")),
        ),
        runtime=RuntimeConfig(
            log_level=str(_require(runtime, "log_level")),
            state_db_path=str(_require(runtime, "state_db_path")),
            backfill_limit=int(_require(runtime, "backfill_limit")),
            dedup_window_days=int(_require(runtime, "dedup_window_days")),
            healthcheck_interval_sec=int(_require(runtime, "healthcheck_interval_sec")),
        ),
    )
