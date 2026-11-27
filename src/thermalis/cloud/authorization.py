"""Kısıtlı veri için bulut depolama yetki kaydı (THERMALIS_AUTHORIZATION.json).

RESTRICTED veya UNKNOWN erişim sınıflı bir paket buluta yalnız bu kayıtla yüklenebilir ve kabul
edilebilir. Kayıt paketin erişim sınıfını DEĞİŞTİRMEZ: RESTRICTED veri RESTRICTED kalır; kayıt yalnız
belirli bir depolama yerinde (sağlayıcı + kova) saklanmasına veri sahibinin izin verdiğini belgeler.

Kayıt belirli bir pakete bağlıdır (veri kimliği, sürüm, paket kimliği ve MANIFEST SHA-256). Başka bir
pakete, kovaya veya süresi geçmiş tarihe ait kayıt kabul edilmez. Kaydın kendisi bir beyandır; imzalı
kurumsal belgenin kendisi ``document_reference`` ile gösterilir ve arşivde ayrıca saklanmalıdır.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "thermalis.cloud-authorization/1"
# ``document_reference`` İSTEĞE BAĞLIDIR: operatör beyanı (``evidence_type: operator_attestation``) geçerli bir kanıt
# türüdür; bir belge yalnız gerçekten varsa (veya dış bir zorunluluk gerektiriyorsa) referans verilir. Eski kayıtlar
# (belge alanı dolu) aynen geçerlidir.
REQUIRED = ("schema", "dataset_id", "version", "package_id", "manifest_sha256", "access_class", "scope", "storage",
            "granted_by", "authority", "granted_at")
NEEDS_AUTHORIZATION = ("RESTRICTED", "UNKNOWN")


class AuthorizationError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _parse_time(value: Any, name: str) -> datetime:
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise AuthorizationError("authorization_invalid_time", name) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def validate(record: Any, *, dataset_id: str, version: str, package_id: str, manifest_sha256: str, access_class: str,
             provider: str, bucket: str, now: datetime | None = None) -> dict[str, Any]:
    """Kaydı doğrular; geçerliyse aynen döndürür, değilse :class:`AuthorizationError`."""
    if not isinstance(record, dict) or record.get("schema") != SCHEMA:
        raise AuthorizationError("authorization_schema_invalid")
    missing = [k for k in REQUIRED if record.get(k) in (None, "", [], {})]
    if missing:
        raise AuthorizationError("authorization_fields_missing", ",".join(missing))
    expected = {"dataset_id": dataset_id, "version": version, "package_id": package_id,
                "manifest_sha256": manifest_sha256, "access_class": access_class}
    for key, value in expected.items():
        if str(record.get(key)) != str(value):
            raise AuthorizationError("authorization_does_not_match_package", key)
    scope = record.get("scope")
    if not isinstance(scope, list) or "cloud_storage" not in scope:
        raise AuthorizationError("authorization_scope_missing_cloud_storage")
    storage = record.get("storage")
    if not isinstance(storage, dict) or storage.get("provider") != provider or storage.get("bucket") != bucket:
        raise AuthorizationError("authorization_storage_mismatch")
    now = now or datetime.now(timezone.utc)
    if _parse_time(record["granted_at"], "granted_at") > now:
        raise AuthorizationError("authorization_not_yet_valid")
    if record.get("expires_at") and _parse_time(record["expires_at"], "expires_at") <= now:
        raise AuthorizationError("authorization_expired")
    return record


def load(path: Path) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError) as exc:
        raise AuthorizationError("authorization_unreadable", type(exc).__name__) from None


def template(*, dataset_id: str, version: str, package_id: str, manifest_sha256: str, access_class: str,
             provider: str, bucket: str) -> dict[str, Any]:
    """Doldurulacak şablon. ``granted_by``/``authority``/``document_reference`` veri sahibinden gelmelidir."""
    return {"schema": SCHEMA, "dataset_id": dataset_id, "version": version, "package_id": package_id,
            "manifest_sha256": manifest_sha256, "access_class": access_class, "scope": ["cloud_storage"],
            "storage": {"provider": provider, "bucket": bucket}, "granted_by": "", "authority": "",
            "document_reference": "", "granted_at": "", "expires_at": None, "conditions": []}
