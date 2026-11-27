"""Yerel kayıtlı bir sürümün buluta taşınması (genel; herhangi bir veri seti için aynı mekanizma).

Taşıma bir YENİDEN KAYIT değildir: yerel değişmez sürüm klasörü olduğu gibi (bayt bayt) yayımlanır.
Yerel kayıt OKUNUR; hiçbir dosyası değiştirilmez, taşınmaz veya silinmez. Etkin depo değişmez: geçiş
ayrı bir onaydır ve o zamana kadar katalog yerel kaydı kullanır (bulut kopyası ``cloud_copies``'te görünür).

Yayımlananlar (``releases/<SEG>/<SEG>/<sürüm>/``):
- ``payload/…``                        integrity.json'daki tüm dosyalar + özetlenmeyen paket dosyaları
- ``records/version.json`` vb.         sürüm, kalite, bütünlük kayıtları ve teslim raporu (bayt bayt aynı)
- ``records/layer_facts.json``         içerik kimlikleri (eski kayıtta yerel GDB'den hesaplanır, önbellekle doğrulanır)
- ``records/quality_identifiers.json`` özel kayıtların GLOBALID/TANITIMKODU değerleri (yerel GDB'den)
- ``records/migration.json``           taşıma kaynağı ve yerel doğrulama özeti (belirlenimci)
- ``records/audits/<kimlik>/…``        bu sürüme ait bağımsız denetim kayıtları
- ``records/semantic_verifications.jsonl`` bu veri setinin katmanlarına ait kullanıcı doğrulamaları (zincir alanlarıyla)
- ``records/reverification_history.jsonl`` yerel yeniden doğrulama geçmişi
- ``records/THERMALIS_AUTHORIZATION.json`` RESTRICTED/UNKNOWN için zorunlu yetki kaydı

Erişim sınıfı değiştirilmez. Kesilen taşıma aynı işlem kimliğiyle (belirlenimci) güvenle sürdürülür.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..data import catalog
from ..data.integrity import ManifestEntry, verify_tree
from ..data.transfer_package import UNHASHED_PACKAGE_FILES
from ..objectstore.base import CancelToken, ObjectStore, sha256_path
from ..objectstore.errors import AlreadyExists, StorageError
from ..objectstore.publication import (PublicationPolicy, ReleaseFile, publish_release, read_commit, reconcile_dataset_pointer,
                                       release_prefix)
from ..storage import read_json
from . import authorization as auth
from . import layout
from .supplementary import SupplementaryError, build_supplementary, canonical

MIGRATION_SCHEMA = "thermalis.release-migration/1"
TOOL = "thermalis-migrate/1"


class MigrationRefused(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


@dataclass
class MigrationPlan:
    dataset_id: str
    version: str
    folder: Path
    package_id: str
    manifest_sha256: str
    access_class: str
    operation_id: str
    files: list[ReleaseFile]
    records: dict[str, bytes]
    authorized: bool
    local_verification: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files) + sum(len(b) for b in self.records.values())


def operation_id_for(dataset_id: str, version: str, manifest_sha256: str) -> str:
    return "mig-" + hashlib.sha256(f"{dataset_id}|{version}|{manifest_sha256}".encode("utf-8")).hexdigest()[:24]


def _jsonl(rows: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode("utf-8")


def plan_migration(data_root: Path, dataset_id: str, version: str, *, provider: str, bucket: str,
                   authorization_path: Path | None = None) -> MigrationPlan:
    """Ağ kullanmadan taşımayı hazırlar ve yerel kaydı TAM doğrular (yalnız okuma)."""
    from ..data import semantics
    from ..data.catalog_index import query_record_issues

    folder = catalog.version_dir(data_root, dataset_id, version)
    if not (folder / "version.json").is_file():
        raise MigrationRefused("local_version_not_found", f"{dataset_id} {version}")
    record = read_json(folder / "version.json")
    if record.get("registration", {}).get("status") != "REGISTERED":
        raise MigrationRefused("local_version_not_registered")
    integrity = read_json(folder / "integrity.json")
    entries = [ManifestEntry(f["path"], f["bytes"], f["sha256"]) for f in integrity["files"]]
    check = verify_tree(folder / "payload", entries, ignore=set(UNHASHED_PACKAGE_FILES))
    if not check.passed:
        raise MigrationRefused("local_integrity_failed", str(check.problems[:5]))
    payload = folder / "payload"
    files = [ReleaseFile(e.path, payload.joinpath(*e.path.split("/")), e.sha256, e.bytes) for e in entries]
    for rel in UNHASHED_PACKAGE_FILES:
        p = payload.joinpath(*rel.split("/"))
        if p.is_file():
            digest, size = sha256_path(p)
            files.append(ReleaseFile(rel, p, digest, size))
    manifest = payload / "PACKAGE" / "MANIFEST.json"
    if not manifest.is_file():
        raise MigrationRefused("manifest_missing")
    manifest_sha, _ = sha256_path(manifest)
    declared = record.get("source", {}).get("package_fingerprint")
    if declared and declared != manifest_sha:
        raise MigrationRefused("fingerprint_mismatch", "version.json source.package_fingerprint ≠ payload MANIFEST")
    package_id = str(record.get("source", {}).get("package_id") or "")
    try:
        layout.validate_package_id(package_id)
    except ValueError:
        raise MigrationRefused("invalid_package_id", package_id) from None
    raw_access = record.get("access", {}).get("classification")
    access = "UNKNOWN" if raw_access in (None, "", "UNSPECIFIED") else str(raw_access)

    authorization = None
    if access in auth.NEEDS_AUTHORIZATION:
        from .grants import authorization_for

        try:  # açık kayıt dosyası (geriye uyumlu) ya da kapsayan merkezi operatör yetkisi
            authorization = authorization_for(data_root, dataset_id=dataset_id, version=version, package_id=package_id,
                                              manifest_sha256=manifest_sha, access_class=access, provider=provider,
                                              bucket=bucket, authorization_path=authorization_path)
        except auth.AuthorizationError as exc:
            raise MigrationRefused(exc.code, exc.detail) from None

    # Ek kayıtlar türetilmiş dizinden okunur; dizin gerekirse sorgu katmanınca kayıtlardan yeniden kurulur.
    records: dict[str, bytes] = {n: (folder / n).read_bytes() for n in ("version.json", "quality.json", "integrity.json")}
    for report in sorted(folder.glob("*.md")):
        records[report.name] = report.read_bytes()
    try:
        records.update(build_supplementary(data_root, folder, record))
    except SupplementaryError as exc:
        raise MigrationRefused(exc.code, exc.detail) from None

    # Denetim geçmişi: bu sürüme ait tüm bağımsız denetim klasörleri (biçimden bağımsız, dosya olduğu gibi).
    audits = []
    audit_root = catalog.data_dir(data_root) / "_audits"
    for audit_dir in sorted(p for p in audit_root.iterdir() if p.is_dir()) if audit_root.is_dir() else []:
        belongs = False
        for js in audit_dir.glob("*.json"):
            try:
                data = read_json(js)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("dataset_id") == dataset_id and data.get("version") == version:
                belongs = True
        if belongs:
            audits.append(audit_dir.name)
            for f in sorted(p for p in audit_dir.rglob("*") if p.is_file()):
                records[f"audits/{audit_dir.name}/{f.relative_to(audit_dir).as_posix()}"] = f.read_bytes()

    # Anlamsal doğrulama geçmişi: yerel zincir değişmeden kalır; bu veri setinin katmanlarına ait satırlar kopyalanır.
    valid, log_state = semantics.read_log(data_root)
    sem_rows = [e for e in valid if str(e.get("dataset_id", "")).startswith(dataset_id + "/")]
    if sem_rows:
        records["semantic_verifications.jsonl"] = _jsonl(sem_rows)
    rever = catalog.data_dir(data_root) / "_ingest" / "verifications.jsonl"
    rever_rows = []
    for line in rever.read_text(encoding="utf-8").splitlines() if rever.is_file() else []:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("dataset_id") == dataset_id and row.get("version") == version:
            rever_rows.append(row)
    if rever_rows:
        records["reverification_history.jsonl"] = _jsonl(rever_rows)
    if authorization is not None:
        records[layout.AUTHORIZATION_NAME] = canonical(authorization)

    issues = sorted((i["code"], i["detail"] or "") for i in query_record_issues(data_root, dataset_id, version))
    verification = {"method": "full_sha256_against_integrity_json", "checked_files": check.checked, "status": "PASS"}
    op = operation_id_for(dataset_id, version, manifest_sha)
    records["migration.json"] = canonical({
        "schema": MIGRATION_SCHEMA, "tool": TOOL, "dataset_id": dataset_id, "version": version, "package_id": package_id,
        "manifest_sha256": manifest_sha, "access_class": access, "access_class_changed": False, "operation_id": op,
        "source": {"backend": "local", "record_prefix": catalog.version_prefix(dataset_id, version)},
        "local_verification": verification, "record_issues_at_migration": [list(i) for i in issues],
        "included": {"audits": audits, "semantic_verifications": len(sem_rows), "reverification_history": len(rever_rows),
                     "supplementary": sorted(k for k in records if k in ("layer_facts.json", "quality_identifiers.json"))},
        "semantic_log_state": {"status": log_state.get("status"), "head": log_state.get("head")},
        "note": "Kayıt yeniden üretilmedi; yerel değişmez sürüm bayt bayt yayımlandı. Yerel kayıt korunur.",
    })
    warnings = [] if declared else ["package_fingerprint_not_recorded_in_version_json (computed from payload MANIFEST)"]
    return MigrationPlan(dataset_id, version, folder, package_id, manifest_sha, access, op, files, records,
                         authorization is not None, verification, warnings)


def migrate_release(store: ObjectStore, plan: MigrationPlan, *, cancel: CancelToken | None = None) -> dict[str, Any]:
    """Planı yayımlar. Sonuç: ``MIGRATED`` | ``ALREADY_MIGRATED``. Çakışmada :class:`MigrationRefused`."""
    entry_key = layout.catalog_entry_key(plan.dataset_id, plan.version)
    prefix = release_prefix(plan.dataset_id, plan.version)
    existing = store.head(entry_key)
    if existing is not None:
        with store.open_read(entry_key) as handle:
            entry = json.loads(handle.read().decode("utf-8"))
        if entry.get("manifest_sha256") == plan.manifest_sha256 and entry.get("operation_id") == plan.operation_id:
            return {"status": "ALREADY_MIGRATED", "release_prefix": prefix, "catalog_entry": entry_key}
        raise MigrationRefused("version_already_in_cloud", "aynı sürüm bulutta başka bir işlemle/içerikle var; üzerine yazılmaz")
    allowed = ("PUBLIC", "SYNTHETIC") + ((plan.access_class,) if plan.authorized else ())
    try:
        commit = publish_release(store, dataset_id=plan.dataset_id, version=plan.version, access_class=plan.access_class,
                                 files=plan.files, records=plan.records, operation_id=plan.operation_id,
                                 policy=PublicationPolicy(allow_cloud_publication=True, allowed_access_classes=allowed), cancel=cancel)
    except AlreadyExists as exc:
        raise MigrationRefused("release_conflict", exc.detail) from None
    quality = json.loads(plan.records["quality.json"].decode("utf-8")).get("status")
    entry = {"schema": layout.CATALOG_ENTRY_SCHEMA, "dataset_id": plan.dataset_id, "version": plan.version,
             "package_id": plan.package_id, "manifest_sha256": plan.manifest_sha256, "release_prefix": prefix,
             "operation_id": commit["operation_id"], "committed_at": commit["committed_at"], "access_class": plan.access_class,
             "quality_status": quality, "object_count": len(commit["objects"]),
             "total_bytes": sum(o["size"] for o in commit["objects"]), "origin": "migration_from_local"}
    try:
        store.put_bytes(entry_key, canonical(entry), create_only=True, operation_id=plan.operation_id, cancel=cancel)
    except AlreadyExists:
        with store.open_read(entry_key) as handle:
            if json.loads(handle.read().decode("utf-8")).get("operation_id") != commit["operation_id"]:
                raise MigrationRefused("catalog_entry_conflict") from None
    reconcile_dataset_pointer(store, plan.dataset_id, operation_id=plan.operation_id)
    return {"status": "MIGRATED", "release_prefix": prefix, "objects": len(commit["objects"]),
            "bytes": entry["total_bytes"], "operation_id": commit["operation_id"]}


def verify_migration(store: ObjectStore, plan: MigrationPlan) -> dict[str, Any]:
    """Taşıma sonrası: işaretteki her payload özeti yerel integrity.json ile birebir mi + tam geri okuma."""
    from ..objectstore.publication import inspect_release

    prefix = release_prefix(plan.dataset_id, plan.version)
    commit = read_commit(store, prefix)
    if commit is None:
        return {"status": "FAIL", "reason": "not_committed"}
    cloud = {o["key"][len(prefix + "payload/"):]: (o["sha256"], o["size"]) for o in commit["objects"]
             if o["key"].startswith(prefix + "payload/")}
    local = {f.relpath: (f.sha256, f.size) for f in plan.files}
    records_ok = all(any(o["key"] == prefix + "records/" + n and o["sha256"] == hashlib.sha256(b).hexdigest()
                         for o in commit["objects"]) for n, b in plan.records.items())
    state = inspect_release(store, prefix, full=True)
    ok = cloud == local and records_ok and state.status == "COMMITTED" and commit["access_class"] == plan.access_class
    return {"status": "PASS" if ok else "FAIL", "payload_files": len(cloud), "payload_identical": cloud == local,
            "records_identical": records_ok, "full_readback": state.status, "access_class": commit["access_class"]}
