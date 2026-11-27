"""Merkezî operatör yetkisi (grant): tek kayıt, açıkça kapsamlanmış birden çok veri seti, kalıcı ve denetlenebilir.

Kavramlar ayrıdır: veri lisansı (paket kaydı), köken (provenance), erişim sınıfı (PUBLIC/RESTRICTED/UNKNOWN),
operatör yetkisi (bu modül), depolama gizliliği (özel kova) ve bilimsel kalite. Bir yetki erişim sınıfını
DEĞİŞTİRMEZ; yalnız belirtilen işlemlerin, belirtilen hedefte, belirtilen sürümler için yapılabileceğini kaydeder.

Kurallar:
- Kapsam AÇIKTIR: her sürüm ``dataset_id`` + ``version`` (+ isteğe bağlı ``manifest_sha256``) ile listelenir; joker yok.
  Kapsam genişletmek yeni bir yetki kaydıdır (mevcut kayıt değişmez → yetkisiz genişleme olamaz).
- Operatör beyanı (``evidence_type: operator_attestation``) geçerli bir kanıt türüdür; belge referansı İSTEĞE BAĞLIDIR.
- Geri alma ayrı, değişmez bir kayıttır; süre sonu (``expires_at``) desteklenir.
- Kayıtlar bulut defterinde (``authorizations``) saklanır; yerel kopya türetilmiştir.
- Geçerli bir yetki varsa yeniden onay istenmez: yayın/taşıma/kabul bu kaydı kullanır ve sürüme özgü
  ``THERMALIS_AUTHORIZATION.json``'u ondan türetir (yetki kimliğiyle).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import authorization as auth
from . import ledger

GRANT_SCHEMA = "thermalis.operator-grant/1"
REVOKE_SCHEMA = "thermalis.operator-grant-revocation/1"
OPERATIONS = ("cloud_storage", "transfer", "thermalis_data_registration", "scientific_analysis_internal")
EVIDENCE_TYPES = ("operator_attestation", "institutional_document")


class GrantError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def _now() -> datetime:
    return datetime.now(timezone.utc)


def build_grant(*, granted_by: str, attestation: str, attestation_date: str, operations: list[str], destinations: list[dict],
                datasets: list[dict], evidence_type: str = "operator_attestation", document_reference: str | None = None,
                conditions: list[str] | None = None, expires_at: str | None = None, created_at: str | None = None) -> dict[str, Any]:
    if evidence_type not in EVIDENCE_TYPES:
        raise GrantError("grant_evidence_type_invalid", evidence_type)
    if evidence_type == "institutional_document" and not document_reference:
        raise GrantError("grant_document_reference_required_for_document_evidence")
    bad = [o for o in operations if o not in OPERATIONS]
    if bad or not operations:
        raise GrantError("grant_operations_invalid", ",".join(bad))
    if not destinations or any(not d.get("provider") or not d.get("bucket") for d in destinations):
        raise GrantError("grant_destination_invalid")
    if not datasets or any(not d.get("dataset_id") or not d.get("version") or "*" in str(d.get("dataset_id")) for d in datasets):
        raise GrantError("grant_dataset_scope_invalid", "explicit dataset_id + version required; no wildcards")
    if not granted_by or not attestation:
        raise GrantError("grant_attestation_missing")
    body = {"schema": GRANT_SCHEMA, "granted_by": granted_by, "attestation": attestation, "attestation_date": attestation_date,
            "evidence_type": evidence_type, "document_reference": document_reference, "operations": sorted(set(operations)),
            "destinations": destinations, "datasets": datasets, "conditions": conditions or [], "expires_at": expires_at,
            "created_at": created_at or _now().strftime("%Y-%m-%dT%H:%M:%SZ")}
    body["id"] = "grant-" + ledger.content_id(body)
    return body


def build_revocation(grant_id: str, *, revoked_by: str, reason: str) -> dict[str, Any]:
    if not reason:
        raise GrantError("revocation_reason_required")
    body = {"schema": REVOKE_SCHEMA, "grant_id": grant_id, "revoked_by": revoked_by, "reason": reason,
            "revoked_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ")}
    body["id"] = "revoke-" + ledger.content_id(body)
    return body


def save(data_root: Path, body: dict[str, Any]) -> Path:
    """Yerel deftere yazar ve itme kuyruğuna alır (bulut eşitlemesinde yalnız-oluştur yazılır)."""
    return ledger.record_local(data_root, "authorizations", body["id"], body)


def all_grants(data_root: Path) -> tuple[list[dict[str, Any]], set[str]]:
    items = ledger.entries(data_root, "authorizations")
    grants = [g for g in items if g.get("schema") == GRANT_SCHEMA and g.get("id") == g.get("_ledger_id")]
    revoked = {r["grant_id"] for r in items if r.get("schema") == REVOKE_SCHEMA}
    return grants, revoked


def status(grant: dict[str, Any], revoked: set[str], now: datetime | None = None) -> str:
    now = now or _now()
    if grant["id"] in revoked:
        return "revoked"
    if grant.get("expires_at"):
        try:
            if datetime.fromisoformat(str(grant["expires_at"]).replace("Z", "+00:00")) <= now:
                return "expired"
        except ValueError:
            return "invalid"
    if ledger.content_id({k: v for k, v in grant.items() if k not in ("id", "_ledger_id")}) != grant["id"][len("grant-"):]:
        return "tampered"
    return "active"


def find(data_root: Path, *, dataset_id: str, version: str, operation: str, provider: str, bucket: str,
         manifest_sha256: str | None = None) -> dict[str, Any] | None:
    """Bu işlemi kapsayan etkin yetki (yoksa ``None``). Kapsam tam eşleşme ister."""
    grants, revoked = all_grants(data_root)
    for g in sorted(grants, key=lambda g: g.get("created_at", ""), reverse=True):
        if status(g, revoked) != "active" or operation not in g.get("operations", []):
            continue
        if not any(d.get("provider") == provider and d.get("bucket") == bucket for d in g.get("destinations", [])):
            continue
        for d in g.get("datasets", []):
            if d.get("dataset_id") == dataset_id and d.get("version") == version and \
                    (not d.get("manifest_sha256") or manifest_sha256 is None or d["manifest_sha256"] == manifest_sha256):
                return {**g, "_scope": d}
    return None


def release_record(grant: dict[str, Any], *, dataset_id: str, version: str, package_id: str, manifest_sha256: str,
                   access_class: str, provider: str, bucket: str) -> dict[str, Any]:
    """Yetkiden türetilen, sürüme bağlı ``THERMALIS_AUTHORIZATION.json`` (mevcut çerçeveyle uyumlu)."""
    scope = grant.get("_scope") or {}
    record = auth.template(dataset_id=dataset_id, version=version, package_id=package_id, manifest_sha256=manifest_sha256,
                           access_class=access_class, provider=provider, bucket=bucket)
    record.update(scope=list(grant["operations"]), granted_by=grant["granted_by"], authority=grant["attestation"],
                  document_reference=grant.get("document_reference"), evidence_type=grant["evidence_type"],
                  granted_at=grant["created_at"], expires_at=grant.get("expires_at"),
                  conditions=list(grant.get("conditions", [])) + list(scope.get("conditions", [])),
                  grant_id=grant["id"], attestation_date=grant.get("attestation_date"))
    return auth.validate(record, dataset_id=dataset_id, version=version, package_id=package_id, manifest_sha256=manifest_sha256,
                         access_class=access_class, provider=provider, bucket=bucket)


def authorization_for(data_root: Path, *, dataset_id: str, version: str, package_id: str, manifest_sha256: str,
                      access_class: str, provider: str, bucket: str, authorization_path: Path | None = None) -> dict[str, Any]:
    """Tek giriş noktası: açık kayıt dosyası verildiyse o (geriye uyumluluk), yoksa kapsayan merkezi yetki."""
    if authorization_path is not None:
        return auth.validate(auth.load(authorization_path), dataset_id=dataset_id, version=version, package_id=package_id,
                             manifest_sha256=manifest_sha256, access_class=access_class, provider=provider, bucket=bucket)
    grant = find(data_root, dataset_id=dataset_id, version=version, operation="cloud_storage", provider=provider, bucket=bucket,
                 manifest_sha256=manifest_sha256)
    if grant is None:
        raise auth.AuthorizationError("authorization_required", f"no active operator grant covers {dataset_id} {version}")
    return release_record(grant, dataset_id=dataset_id, version=version, package_id=package_id, manifest_sha256=manifest_sha256,
                          access_class=access_class, provider=provider, bucket=bucket)
