"""R2 anahtar düzeni. Tüm anahtarlar isteğe bağlı bir ad alanının (namespace) altındadır.

    incoming/<paket_id>/<göreli yol>                  ikinci bilgisayarın yüklediği paket (yalnız-oluştur)
    incoming/<paket_id>/THERMALIS_AUTHORIZATION.json  (kısıtlı veri) bulut depolama yetki kaydı
    incoming/<paket_id>/THERMALIS_READY.json          tamamlanma işareti — EN SON yazılır
    incoming/_ready/<paket_id>.json                   duyuru: keşif yalnız bu küçük öneki listeler
    releases/<SEG>/<SEG>/<sürüm>/...                  değişmez sürüm (bkz. objectstore.publication)
    catalog/commits/<SEG>/<SEG>/<sürüm>.json          merkezi katalog girdisi (işaretin kopyası; artımlı eşitleme)
    state/intake/<paket_id>/<manifest_özeti>.json     kabul durumu (talep/kilit, KABUL, RED)
    state/publish/<SEG>/<SEG>/<sürüm>.json            kaynak klasöründen doğrudan yayının talebi ve sonucu
    state/datasets/<SEG>/<SEG>/releases.json          türetilmiş sürüm işaretçisi

Yükleyici yalnız ``incoming/`` altına yazar. Yayın, katalog ve durum yalnız yayımlayıcı (bu bilgisayar)
tarafından yazılır. Ayrı kova kullanılırsa ``incoming/`` kabul kovasında, diğerleri ana kovada durur.
"""

from __future__ import annotations

import re

from ..objectstore.keys import validate_key

INCOMING = "incoming/"
READY_INDEX = "incoming/_ready/"
READY_NAME = "THERMALIS_READY.json"
AUTHORIZATION_NAME = "THERMALIS_AUTHORIZATION.json"
CATALOG_COMMITS = "catalog/commits/"
INTAKE_STATE = "state/intake/"
PUBLISH_STATE = "state/publish/"
PUBLISH_STATE_SCHEMA = "thermalis.direct-publication-state/1"
RESERVED_PACKAGE_FILES = (READY_NAME, AUTHORIZATION_NAME)
UPLOAD_SCHEMA = "thermalis.cloud-upload/1"
INTAKE_STATE_SCHEMA = "thermalis.cloud-intake-state/1"
CATALOG_ENTRY_SCHEMA = "thermalis.catalog-entry/1"
_PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


def validate_package_id(package_id: str) -> str:
    if not isinstance(package_id, str) or not _PACKAGE_ID.match(package_id) or package_id.endswith("."):
        raise ValueError(f"invalid package id: {package_id!r}")
    return package_id


def package_prefix(package_id: str) -> str:
    return INCOMING + validate_package_id(package_id) + "/"


def ready_key(package_id: str) -> str:
    return package_prefix(package_id) + READY_NAME


def authorization_key(package_id: str) -> str:
    return package_prefix(package_id) + AUTHORIZATION_NAME


def announcement_key(package_id: str) -> str:
    return READY_INDEX + validate_package_id(package_id) + ".json"


def catalog_entry_key(dataset_id: str, version: str) -> str:
    from ..data.catalog import dataset_segments

    return validate_key(CATALOG_COMMITS + "/".join(dataset_segments(dataset_id)) + f"/{version}.json")


def intake_state_key(package_id: str, manifest_sha256: str) -> str:
    return validate_key(INTAKE_STATE + validate_package_id(package_id) + f"/{manifest_sha256[:32]}.json")


def publish_state_key(dataset_id: str, version: str) -> str:
    """Kaynak klasöründen doğrudan yayının talebi/sonucu (tek yayımlayıcı; yenilenebilir talep)."""
    from ..data.catalog import dataset_segments

    return validate_key(PUBLISH_STATE + "/".join(dataset_segments(dataset_id)) + f"/{version}.json")
