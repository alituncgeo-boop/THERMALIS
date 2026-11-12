"""Makineye özel ayarlar: dil, veri kökü, aktif proje.

Ayarlar kullanıcının yapılandırma klasöründe tutulur (repoda değil).
Testler ve özel kurulumlar için ``THERMALIS_CONFIG_DIR`` ortam değişkeni kullanılabilir.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from platformdirs import user_config_dir
from pydantic import BaseModel

from .storage import read_json, write_json_atomic

Language = Literal["tr", "en"]
SETTINGS_SCHEMA_VERSION = "1.0"


class Settings(BaseModel):
    schema_version: str = SETTINGS_SCHEMA_VERSION
    language: Language = "tr"
    data_root: str | None = None
    active_project_id: str | None = None
    # Gelen aktarım paketleri klasörü (makineye özel; kodda sabit yol yok).
    transfer_incoming_dir: str | None = None


def config_dir() -> Path:
    override = os.environ.get("THERMALIS_CONFIG_DIR")
    if override:
        return Path(override)
    return Path(user_config_dir("THERMALIS", appauthor=False))


def settings_path() -> Path:
    return config_dir() / "settings.json"


def default_data_root() -> Path:
    return Path.home() / "THERMALIS_DATA"


def load_settings() -> Settings:
    path = settings_path()
    if not path.exists():
        return Settings()
    return Settings.model_validate(read_json(path))


def save_settings(settings: Settings) -> None:
    write_json_atomic(settings_path(), settings.model_dump())


def effective_data_root(settings: Settings) -> Path:
    return Path(settings.data_root) if settings.data_root else default_data_root()
