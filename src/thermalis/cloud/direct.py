"""Kaynak klasöründen doğrudan bulut kaydı ve yayını (yerel tam hazırlık kopyası OLUŞTURULMAZ).

Büyük raster koleksiyonları (ör. 62 GB HD02) için: kaynak paket olduğu yerde okunur, dizinlenir ve nesneler
doğrudan kaynak dosyalardan yüklenir. Yerelde yalnız küçük kayıt ve günlük dosyaları yazılır (çalışma klasörü).

Adımlar (her biri yeniden başlatılabilir; aynı belirlenimci işlem kimliği ``col-<özet>``):

1. Plan (ağsız): manifest + paket dışı beyan + biçim ön denetimi + tek geçişli SHA-256/üye dizini → kayıtlar.
2. Talep: ``state/publish/<SEG>/<SEG>/<sürüm>.json`` yenilenebilir talep (+ yerel kalp atışlı kilit).
3. Yayın: :func:`publish_release` — zaten yazılmış nesneler atlanır, çok parçalı yüklemeler sürdürülür, tam geri okuma
   doğrulaması günlüğe yazılır; ``_COMMIT.json`` en son.
4. Katalog girdisi + sürüm işaretçisi + talep sonucu (ACCEPTED) + yerel katalog eşitlemesi.

Kaynak dosyalar hiçbir aşamada değiştirilmez, taşınmaz veya silinmez. Erişim sınıfı beyandan gelir ve
RESTRICTED/UNKNOWN için ayrıca geçerli bulut yetki kaydı gerekir.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from ..data import catalog
from ..data import declaration as decl
from ..data.collection import BuiltCollection, CollectionError, SourcePackage, build_records, load_source_package
from ..objectstore.base import CancelToken, ObjectStore
from ..objectstore.errors import AlreadyExists
from ..objectstore.publication import (PublicationPolicy, ReleaseFile, publish_release, read_commit, reconcile_dataset_pointer,
                                       release_prefix)
from . import authorization as auth
from . import layout
from .claims import Busy, LocalLock, RenewableClaim

ORIGIN = "direct_publication_from_source"


class DirectRefused(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


@dataclass
class CollectionPlan:
    dataset_id: str
    version: str
    package_id: str
    manifest_sha256: str
    access_class: str
    operation_id: str
    source_root: Path
    work: Path
    files: list[ReleaseFile]
    records: dict[str, bytes]
    built: BuiltCollection
    authorized: bool
    warnings: list[str] = field(default_factory=list)

    @property
    def payload_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def total_bytes(self) -> int:
        return self.payload_bytes + sum(len(b) for b in self.records.values())

    def summary(self) -> dict[str, Any]:
        quality = json.loads(self.records["quality.json"])
        return {"dataset_id": self.dataset_id, "version": self.version, "package_id": self.package_id,
                "manifest_sha256": self.manifest_sha256, "access_class": self.access_class, "operation_id": self.operation_id,
                "payload_files": len(self.files), "payload_bytes": self.payload_bytes, "records": sorted(self.records),
                "record_bytes": sum(len(b) for b in self.records.values()), "items": self.built.item_count,
                "assets": self.built.asset_count, "raster_assets": self.built.raster_asset_count,
                "quality_status": quality["status"], "source_status": quality["source_status"],
                "issues": self.built.issues[:50], "issue_count": len(self.built.issues), "records_reused": self.built.reused,
                "work_dir": str(self.work), "warnings": self.warnings}


def operation_id_for(dataset_id: str, version: str, manifest_sha256: str) -> str:
    return "col-" + hashlib.sha256(f"{dataset_id}|{version}|{manifest_sha256}".encode("utf-8")).hexdigest()[:24]


def work_dir_for(work_root: Path, package_id: str, manifest_sha256: str) -> Path:
    return Path(work_root) / f"c-{hashlib.sha256(f'{package_id}|{manifest_sha256}'.encode('utf-8')).hexdigest()[:12]}"


def plan_collection(source_root: Path, declaration: dict[str, Any] | Path, *, data_root: Path, work_root: Path, provider: str,
                    bucket: str, authorization_path: Path | None = None, cancel: CancelToken | None = None,
                    progress: Callable[[str, int, int], None] | None = None,
                    log: Callable[[str], None] | None = None, package: SourcePackage | None = None) -> CollectionPlan:
    """Ağ kullanmadan yayını hazırlar. Kaynak yalnız okunur; çalışma klasörüne yalnız küçük JSON yazılır.

    ``package``: önceden yüklenmiş paket görünümü (ör. kabul testinin kopyasız alt kümesi); verilmezse klasörden okunur.
    """
    from ..data.bundle import build_bundle_records, is_bundle, load_bundle

    try:
        pkg = package or (load_bundle(Path(source_root)) if is_bundle(Path(source_root)) else load_source_package(Path(source_root)))
    except CollectionError as exc:
        raise DirectRefused(exc.code, exc.detail) from None
    try:
        d = decl.load(declaration, manifest_sha256=pkg.manifest_sha256, package_id=pkg.package_id) \
            if isinstance(declaration, Path) else decl.validate(declaration, manifest_sha256=pkg.manifest_sha256,
                                                                package_id=pkg.package_id)
    except decl.DeclarationError as exc:
        raise DirectRefused(exc.code, exc.detail) from None
    ds, ver, access = d["dataset_id"], d["version"], d["access_class"]
    if (catalog.version_dir(data_root, ds, ver) / "version.json").is_file():
        raise DirectRefused("already_registered_locally", f"{ds} {ver}: depolar arası yineleme yapılmaz")
    authorization = None
    if access in auth.NEEDS_AUTHORIZATION:
        if authorization_path is None:
            raise DirectRefused("authorization_required", access)
        try:
            authorization = auth.validate(auth.load(authorization_path), dataset_id=ds, version=ver, package_id=pkg.package_id,
                                          manifest_sha256=pkg.manifest_sha256, access_class=access, provider=provider, bucket=bucket)
        except auth.AuthorizationError as exc:
            raise DirectRefused(exc.code, exc.detail) from None
    op = operation_id_for(ds, ver, pkg.manifest_sha256)
    work = work_dir_for(work_root, pkg.package_id, pkg.manifest_sha256)
    cancel_fn = (lambda: cancel.raise_if_cancelled(None)) if cancel is not None else None
    builder = build_bundle_records if pkg.kind == "zip_bundle" else build_records
    try:
        built = builder(pkg, d, work, ingest_id=op, cancel=cancel_fn, progress=progress, log=log)
    except CollectionError as exc:
        raise DirectRefused(exc.code, exc.detail) from None
    records = dict(built.records)
    if authorization is not None:
        records[layout.AUTHORIZATION_NAME] = (json.dumps(authorization, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")
    files = [ReleaseFile(e.path, pkg.path(e.path), e.sha256, e.bytes, verified=True) for e in pkg.entries]
    for rel in pkg.extra_files:  # özetlenmeyen paket dosyaları (MANIFEST.json): küçük, burada hesaplanır
        p = pkg.path(rel)
        files.append(ReleaseFile(rel, p, hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_size))
    warnings = []
    supp = d.get("supplementary_records") or {}
    if str(supp.get("license", "")).lower() in ("", "not_declared", "unknown"):
        warnings.append("supplementary_records_license_not_declared")
    return CollectionPlan(ds, ver, pkg.package_id, pkg.manifest_sha256, access, op, pkg.root, work, files, records, built,
                          authorization is not None, warnings)


def publish_collection(store: ObjectStore, plan: CollectionPlan, *, machine: str, claim_ttl_hours: float = 12.0,
                       cancel: CancelToken | None = None, progress: Callable[[str, int, int], None] | None = None) -> dict[str, Any]:
    """Planı yayımlar. Sonuç ``PUBLISHED`` | ``ALREADY_PUBLISHED``. Çakışmada :class:`DirectRefused`."""
    entry_key = layout.catalog_entry_key(plan.dataset_id, plan.version)
    prefix = release_prefix(plan.dataset_id, plan.version)
    if store.head(entry_key) is not None:
        with store.open_read(entry_key) as handle:
            entry = json.loads(handle.read().decode("utf-8"))
        if entry.get("manifest_sha256") == plan.manifest_sha256 and entry.get("operation_id") == plan.operation_id:
            return {"status": "ALREADY_PUBLISHED", "release_prefix": prefix, "catalog_entry": entry_key}
        raise DirectRefused("version_already_in_cloud", "aynı sürüm bulutta başka içerik/işlemle var; üzerine yazılmaz")
    changed = source_changes_since_index(plan)
    if changed:
        raise DirectRefused("source_changed_since_indexing", ", ".join(changed[:5]) + " — planı yeniden çalıştırın")
    lock = LocalLock(plan.work / "publish.lock")
    if not lock.acquire():
        raise DirectRefused("busy_local", "aynı yayın bu bilgisayarda başka bir süreçte sürüyor")
    try:
        claim = RenewableClaim(store, layout.publish_state_key(plan.dataset_id, plan.version), operation_id=plan.operation_id,
                               machine=machine, schema=layout.PUBLISH_STATE_SCHEMA, ttl=timedelta(hours=claim_ttl_hours),
                               extra={"dataset_id": plan.dataset_id, "version": plan.version, "package_id": plan.package_id,
                                      "manifest_sha256": plan.manifest_sha256})
        try:
            state, terminal = claim.acquire()
        except Busy as exc:
            raise DirectRefused("claimed_elsewhere", str(exc)) from None
        if state == "terminal" and terminal is not None and terminal.get("status") == "ACCEPTED" \
                and terminal.get("operation_id") == plan.operation_id:
            existing = read_commit(store, prefix)
            if existing is not None:
                return {"status": "ALREADY_PUBLISHED", "release_prefix": prefix}
        if state == "terminal":
            raise DirectRefused("publication_state_terminal", json.dumps(terminal, ensure_ascii=False)[:300])

        def beat() -> None:
            lock.touch()
            claim.renew()

        allowed = ("PUBLIC", "SYNTHETIC") + ((plan.access_class,) if plan.authorized else ())
        try:
            commit = publish_release(store, dataset_id=plan.dataset_id, version=plan.version, access_class=plan.access_class,
                                     files=plan.files, records=plan.records, operation_id=plan.operation_id,
                                     policy=PublicationPolicy(allow_cloud_publication=True, allowed_access_classes=allowed),
                                     cancel=cancel, journal=plan.work / "verification-journal.json", progress=progress,
                                     heartbeat=beat)
        except AlreadyExists as exc:
            raise DirectRefused("release_conflict", exc.detail) from None
        quality = json.loads(plan.records["quality.json"].decode("utf-8")).get("status")
        entry = {"schema": layout.CATALOG_ENTRY_SCHEMA, "dataset_id": plan.dataset_id, "version": plan.version,
                 "package_id": plan.package_id, "manifest_sha256": plan.manifest_sha256, "release_prefix": prefix,
                 "operation_id": commit["operation_id"], "committed_at": commit["committed_at"], "access_class": plan.access_class,
                 "quality_status": quality, "object_count": len(commit["objects"]),
                 "total_bytes": sum(o["size"] for o in commit["objects"]), "origin": ORIGIN, "record_kind": "raster_collection"}
        try:
            store.put_bytes(entry_key, json_bytes(entry), create_only=True, operation_id=plan.operation_id, cancel=cancel)
        except AlreadyExists:
            with store.open_read(entry_key) as handle:
                if json.loads(handle.read().decode("utf-8")).get("operation_id") != commit["operation_id"]:
                    raise DirectRefused("catalog_entry_conflict") from None
        reconcile_dataset_pointer(store, plan.dataset_id, operation_id=plan.operation_id)
        claim.finish("ACCEPTED", dataset_id=plan.dataset_id, version=plan.version, package_id=plan.package_id,
                     manifest_sha256=plan.manifest_sha256, release_prefix=prefix, quality_status=quality,
                     access_class=plan.access_class, origin=ORIGIN)
        return {"status": "PUBLISHED", "release_prefix": prefix, "objects": len(commit["objects"]),
                "bytes": entry["total_bytes"], "operation_id": commit["operation_id"]}
    finally:
        lock.release()


def source_changes_since_index(plan: CollectionPlan) -> list[str]:
    """SHA-256'sı dizin sırasında doğrulanmış (``verified``) dosyaların boyut/zamanı o andan beri değişti mi?

    Yükleme bu dosyaları yeniden özetlemez; bu yüzden kuru çalıştırma ile yayın arasında değişen dosya yayımlanmaz.
    """
    from ..data.collection import _journal_path, _stat_sig
    from ..storage import read_json

    changed = []
    for f in plan.files:
        if not f.verified:
            continue
        journal = _journal_path(plan.work, f.relpath)
        try:
            recorded = read_json(journal).get("stat")
        except (OSError, ValueError):
            recorded = None
        if recorded is None or _stat_sig(f.path) != recorded or f.size != recorded.get("size"):
            changed.append(f.relpath)
    return changed


def json_bytes(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def verify_collection(store: ObjectStore, plan: CollectionPlan, *, full: bool = True) -> dict[str, Any]:
    """Yayın sonrası: işaretteki her payload özeti manifestle birebir mi, kayıtlar aynı mı + (isteğe bağlı) tam geri okuma."""
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
    state = inspect_release(store, prefix, full=full)
    ok = cloud == local and records_ok and state.status == "COMMITTED" and commit["access_class"] == plan.access_class
    return {"status": "PASS" if ok else "FAIL", "payload_files": len(cloud), "payload_identical": cloud == local,
            "records_identical": records_ok, "readback": state.status, "readback_level": "full" if full else "metadata",
            "access_class": commit["access_class"]}
