"""Makineye özel ayarlar: dil, veri kökü, aktif proje.

Ayarlar kullanıcının yapılandırma klasöründe tutulur (repoda değil).
Testler ve özel kurulumlar için ``THERMALIS_CONFIG_DIR`` ortam değişkeni kullanılabilir.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from platformdirs import user_config_dir
from pydantic import BaseModel, Field

from .storage import read_json, write_json_atomic

Language = Literal["tr", "en"]
SETTINGS_SCHEMA_VERSION = "1.0"


class CloudSettings(BaseModel):
    """S3 uyumlu bulut deposu (Cloudflare R2). GİZLİ DEĞER İÇERMEZ: kimlik bilgileri OS kimlik deposundadır.

    Uç nokta ve kova makineye özel ayardır; kodda hesap kimliği veya uç nokta sabit yazılmaz.
    Bu ayarların varlığı bulutu etkinleştirmez: uygulamanın veri deposu ``storage_backend`` ile seçilir.
    """

    provider: Literal["r2"] = "r2"
    endpoint: str | None = None
    bucket: str | None = None
    region: str = "auto"
    credential_profile: str = Field(default="default", pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    connect_timeout_seconds: float = Field(default=10.0, ge=1, le=120)
    read_timeout_seconds: float = Field(default=60.0, ge=1, le=600)
    max_attempts: int = Field(default=5, ge=1, le=10)
    multipart_part_size_mib: int = Field(default=64, ge=5, le=5120)


class CacheSettings(BaseModel):
    """Yerel nesne önbelleği (atılabilir). ``cache_dir`` boşsa kullanıcı önbellek klasörü kullanılır."""

    cache_dir: str | None = None
    max_bytes: int = Field(default=20 * 1024 ** 3, ge=1024 * 1024)


class Settings(BaseModel):
    schema_version: str = SETTINGS_SCHEMA_VERSION
    language: Language = "tr"
    data_root: str | None = None
    active_project_id: str | None = None
    # Gelen aktarım paketleri klasörü (makineye özel; kodda sabit yol yok).
    transfer_incoming_dir: str | None = None
    # Uygulama açıkken klasörün aralıklı taranması.
    transfer_watch_enabled: bool = True
    transfer_scan_interval_seconds: int = Field(default=30, ge=10, le=3600)
    # Hazır işareti olmayan paketlerde son değişiklikten sonra beklenecek süre.
    transfer_settle_seconds: int = Field(default=120, ge=0, le=86400)
    # Kayıtlı sürümlerin deposu. Açık geçiş onayına kadar yalnız "local" geçerlidir.
    storage_backend: Literal["local"] = "local"
    cloud: CloudSettings = Field(default_factory=CloudSettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)


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


def component_token_path() -> Path:
    return config_dir() / "component_token"


def component_token() -> str:
    """Yerel THERMALIS bileşenlerinin kısıtlı veri erişim belirteci.

    Kullanıcının yapılandırma klasöründe tutulur (repoda ve veri kökünde değil); aynı kullanıcının
    yerel bileşenleri okuyabilir, tarayıcıdaki sayfalar okuyamaz. İlk istekte rastgele üretilir.
    """
    path = component_token_path()
    if path.is_file():
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    import secrets

    value = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows'ta izin kullanıcı profiliyle sınırlıdır
        pass
    return value


def effective_data_root(settings: Settings) -> Path:
    return Path(settings.data_root) if settings.data_root else default_data_root()
