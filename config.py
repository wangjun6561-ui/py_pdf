from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


@dataclass
class TelegramConfig:
    api_id: int
    api_hash: str
    phone_number: str
    twofa_password: str
    session_path: str


@dataclass
class ChannelsConfig:
    targets: List[str]


@dataclass
class NotifyConfig:
    serverchan_sendkey: str
    title_prefix: str
    max_text_len: int


@dataclass
class RuntimeConfig:
    log_level: str
    state_db_path: str
    backfill_limit: int
    dedup_window_days: int
    healthcheck_interval_sec: int
    public_poll_interval_sec: int


@dataclass
class AppConfig:
    telegram: TelegramConfig
    channels: ChannelsConfig
    notify: NotifyConfig
    runtime: RuntimeConfig


class ConfigError(ValueError):
    pass


def _require(data: Dict[str, Any], key: str) -> Any:
    if key not in data:
        raise ConfigError(f"Missing config key: {key}")
    return data[key]


def _as_dict(value: Any, key: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"Config key must be mapping: {key}")
    return value


def _as_list(value: Any, key: str) -> List[Any]:
    if not isinstance(value, list):
        raise ConfigError(f"Config key must be list: {key}")
    return value


def load_config(path: str = "config.yaml") -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ConfigError("Top-level config must be a mapping")

    telegram = _as_dict(_require(raw, "telegram"), "telegram")
    channels = _as_dict(_require(raw, "channels"), "channels")
    notify = _as_dict(_require(raw, "notify"), "notify")
    runtime = _as_dict(_require(raw, "runtime"), "runtime")

    return AppConfig(
        telegram=TelegramConfig(
            api_id=int(telegram.get("api_id", 0) or 0),
            api_hash=str(telegram.get("api_hash", "") or ""),
            phone_number=str(telegram.get("phone_number", "") or ""),
            twofa_password=str(telegram.get("twofa_password", "") or ""),
            session_path=str(_require(telegram, "session_path")),
        ),
        channels=ChannelsConfig(
            targets=[str(item) for item in _as_list(_require(channels, "targets"), "channels.targets")],
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
            public_poll_interval_sec=int(runtime.get("public_poll_interval_sec", 15)),
        ),
    )


def can_use_telethon(cfg: TelegramConfig) -> bool:
    return bool(cfg.api_id and cfg.api_hash and cfg.phone_number)
