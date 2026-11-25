"""Buluttaki hazır paketlerin kabulü (bu bilgisayarda; tek yayımlayıcı).

Akış (her paket için; mevcut bilimsel QA ve kayıt mantığı AYNEN kullanılır):

1. Keşif: yalnız ``incoming/_ready/`` duyuruları listelenir (sayfalı; tüm kova indirilmez/listelenmez).
   Tamamlanma işareti olmayan paket görünmez. Yerel durumda sonuçlanmış paket yeniden işlenmez.
2. İşaret: ``THERMALIS_READY.json`` okunur ve doğrulanır (şema, paket kimliği, dosya listesi, MANIFEST özeti).
3. Talep: ``state/intake/<paket>/<özet>.json`` yalnız-oluştur ile alınır (iki işlemci aynı paketi işleyemez;
   süresi geçmiş talep If-Match ile devralınır). Sonuç (KABUL/RED) aynı nesneye yazılır.
4. Yineleme: aynı sürüm bulutta (katalog girdisi) veya yerelde kayıtlıysa yeniden kaydedilmez.
5. Yetki: RESTRICTED/UNKNOWN paket yalnız geçerli ``THERMALIS_AUTHORIZATION.json`` ile kabul edilir.
6. Alan: hazırlık alanı (önbellekten ayrı) ve disk boşluğu denetlenir; yetmezse ERTELENİR (reddedilmez).
7. İndirme: her dosya akış hâlinde indirilir ve SHA-256 ile doğrulanır; kesilirse doğrulanmış dosyalar korunur.
8. Kayıt: :func:`thermalis.data.ingest.ingest_path` yalıtılmış geçici bir veri kökünde çalışır (tüm kapılar, QA).
9. Yayın: kabul edilen sürüm :func:`publish_release` ile yazılır; her nesne geri okunup doğrulanmadan
   ``_COMMIT.json`` yazılmaz. Ardından katalog girdisi, sürüm işaretçisi ve KABUL durumu.
10. Katalog: işaretli sürüm yerel yansıya eşitlenir; geçici alan yalnız başarılı yayın ve doğrulamadan sonra silinir.

Kaynak paket R2'de değiştirilmez/silinmez; reddedilen paket ``incoming/`` altında kalır (yaşam döngüsü kuralıyla temizlenir).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..data import catalog
from ..data.transfer_package import UNHASHED_PACKAGE_FILES
from ..objectstore.base import CancelToken, ObjectStore, is_sha256_hex, sha256_path
from ..objectstore.errors import AlreadyExists, NotFound, PreconditionFailed, StorageError, TransientError
from ..objectstore.keys import validate_key
from ..objectstore.publication import PublicationPolicy, ReleaseFile, publish_release, reconcile_dataset_pointer
from ..storage import read_json, write_json_atomic
from . import authorization as auth
from . import layout
from .catalog_sync import mirror_folder, sync_catalog

MIB = 1024 * 1024
STAGING_MARGIN = 256 * MIB
WINDOWS_PATH_LIMIT = 250  # MAX_PATH (260) altında güvenli pay; GDAL uzun yol önekini her yerde desteklemez
TERMINAL = ("ACCEPTED", "REJECTED")


class Rejected(Exception):
    def __init__(self, code: str, detail: str = "", report: dict | None = None) -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail, self.report = code, detail, report or {}


class Deferred(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class Busy(Exception):
    pass


@dataclass
class PackageOutcome:
    package_id: str
    status: str  # ACCEPTED | REJECTED | DEFERRED | BUSY | FAILED_TRANSIENT | SKIPPED
    code: str = ""
    detail: str = ""
    dataset_id: str | None = None
    version: str | None = None


@dataclass
class CloudScanResult:
    status: str  # ok | offline | error | busy
    discovered: int = 0
    outcomes: list[PackageOutcome] = field(default_factory=list)
    error: str | None = None
    sync: dict[str, Any] | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def _read_json(store: ObjectStore, key: str) -> Any:
    with store.open_read(key) as handle:
        raw = handle.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Rejected("marker_invalid", f"not JSON: {key}") from None


def machine_tag() -> str:
    return hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()[:8]


def validate_marker(marker: Any, package_id: str) -> dict[str, Any]:
    if not isinstance(marker, dict) or marker.get("schema") != layout.UPLOAD_SCHEMA:
        raise Rejected("marker_invalid", "schema")
    if marker.get("package_id") != package_id or not is_sha256_hex(marker.get("manifest_sha256")):
        raise Rejected("marker_invalid", "identity")
    files = marker.get("files")
    if not isinstance(files, list) or not files:
        raise Rejected("marker_invalid", "files")
    seen = set()
    for f in files:
        if not isinstance(f, dict) or not is_sha256_hex(f.get("sha256")) or not isinstance(f.get("bytes"), int) or f["bytes"] < 0:
            raise Rejected("marker_invalid", "file_entry")
        try:
            validate_key(f.get("path", ""))
        except StorageError:
            raise Rejected("marker_invalid", f"unsafe_path:{str(f.get('path'))[:80]}") from None
        if f["path"] in layout.RESERVED_PACKAGE_FILES or f["path"] in seen:
            raise Rejected("marker_invalid", f"reserved_or_duplicate:{f['path']}")
        seen.add(f["path"])
    if "PACKAGE/MANIFEST.json" not in seen:
        raise Rejected("marker_invalid", "manifest_missing")
    if marker.get("total_bytes") != sum(f["bytes"] for f in files):
        raise Rejected("marker_invalid", "total_bytes")
    return marker


class CloudIntake:
    def __init__(self, intake_store: ObjectStore, release_store: ObjectStore, data_root: Path, *, provider: str, bucket: str,
                 intake_bucket: str, namespace: str, staging_root: Path, staging_max_bytes: int, claim_ttl_hours: float = 12.0,
                 max_packages: int = 2, disk_free: Callable[[Path], int] | None = None, now: Callable[[], datetime] = _now) -> None:
        self.intake_store, self.release_store = intake_store, release_store
        self.data_root = Path(data_root)
        self.provider, self.bucket, self.intake_bucket, self.namespace = provider, bucket, intake_bucket, namespace
        self.staging_root = Path(staging_root)
        self.staging_max_bytes = staging_max_bytes
        self.claim_ttl = timedelta(hours=claim_ttl_hours)
        self.max_packages = max_packages
        self.disk_free = disk_free or (lambda p: shutil.disk_usage(p).free)
        self.now = now
        self.machine = machine_tag()

    # -- yerel durum (intake.json; arayüzün okuduğu) ------------------------------------------------
    def _local_key(self, package_id: str) -> str:
        return f"r2://{self.intake_bucket}/{self.namespace}{layout.package_prefix(package_id)}"

    def _local(self, package_id: str, state: str, reason: str = "", code: str = "", **extra: Any) -> None:
        from ..data.ingest import _PROCESS_LOCK, _save_intake, _set_state, load_intake

        with _PROCESS_LOCK:
            intake = load_intake(self.data_root)
            before = json.dumps(intake, sort_keys=True, ensure_ascii=False)
            _set_state(intake, None, state, reason, code, key=self._local_key(package_id), name=package_id, source="cloud",
                       package_id=package_id, **extra)
            if json.dumps(intake, sort_keys=True, ensure_ascii=False) != before:
                _save_intake(self.data_root, intake)

    def _local_entry(self, package_id: str) -> dict[str, Any] | None:
        from ..data.ingest import load_intake

        return load_intake(self.data_root)["packages"].get(self._local_key(package_id))

    # -- tarama ------------------------------------------------------------------------------------
    def scan(self, cancel: CancelToken | None = None) -> CloudScanResult:
        """Süreçler arası tek tarama (uygulama + komut satırı aynı anda çalışırsa biri bekler)."""
        lock = self.staging_root / "cloud-intake.lock"
        self.staging_root.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                stale = (self.now().timestamp() - lock.stat().st_mtime) > self.claim_ttl.total_seconds()
            except OSError:
                stale = False
            if not stale:
                return CloudScanResult("busy")
            lock.unlink(missing_ok=True)  # çökme sonrası kalan eski kilit (talep süresinden uzun)
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        try:
            return self._scan(cancel)
        finally:
            lock.unlink(missing_ok=True)

    def _scan(self, cancel: CancelToken | None) -> CloudScanResult:
        result = CloudScanResult("ok")
        try:
            announcements = [i for i in self.intake_store.list(layout.READY_INDEX) if i.key.endswith(".json")]
            result.discovered = len(announcements)
            processed = 0
            for info in announcements:
                package_id = info.key[len(layout.READY_INDEX):-len(".json")]
                try:
                    layout.validate_package_id(package_id)
                except ValueError:
                    continue
                local = self._local_entry(package_id)
                if local and local.get("announcement_etag") == info.etag and local.get("cloud_state") in TERMINAL:
                    continue  # sonuçlanmış ve değişmemiş: ağ isteği yok, günlük yok
                if processed >= self.max_packages:
                    break
                processed += 1
                result.outcomes.append(self.process(package_id, info.etag, cancel))
        except StorageError as exc:
            result.status = "offline" if exc.retryable else "error"
            result.error = f"{exc.code}:{exc.detail}" if exc.detail else exc.code
            return result
        sync = sync_catalog(self.release_store, self.data_root, provider=self.provider, bucket=self.bucket, namespace=self.namespace,
                            cancel=cancel)
        result.sync = {"status": sync.status, "mirrored": sync.mirrored, "unchanged": sync.unchanged, "problems": sync.problems,
                       "release_count": sync.release_count, "committed_bytes": sync.committed_bytes, "error": sync.error}
        if sync.status != "ok" and result.status == "ok":
            result.status, result.error = sync.status, sync.error
        return result

    # -- talep (claim) -------------------------------------------------------------------------------
    def _claim(self, state_key: str, op: str) -> tuple[str, dict[str, Any] | None]:
        """("claimed", None) | ("terminal", durum) — meşgulse :class:`Busy`."""
        body = {"schema": layout.INTAKE_STATE_SCHEMA, "status": "PROCESSING", "operation_id": op, "machine": self.machine,
                "claimed_at": _iso(self.now())}
        head = self.release_store.head(state_key)
        if head is None:
            try:
                self.release_store.put_bytes(state_key, _canonical(body), create_only=True, operation_id=op)
                return "claimed", None
            except AlreadyExists:
                head = self.release_store.head(state_key)
        current = _read_json(self.release_store, state_key)
        if current.get("status") in TERMINAL:
            return "terminal", current
        if current.get("operation_id") == op:
            return "claimed", None  # kendi yarım işimiz: sürdür
        claimed = datetime.fromisoformat(str(current.get("claimed_at", "1970-01-01T00:00:00Z")).replace("Z", "+00:00"))
        if self.now() - claimed < self.claim_ttl:
            raise Busy(current.get("machine"))
        try:  # süresi geçmiş talep: koşullu devralma (yarışta tek kazanan)
            self.release_store.put_bytes(state_key, _canonical({**body, "taken_over_from": current.get("operation_id")}),
                                         if_match=head.etag, operation_id=op)
        except PreconditionFailed:
            raise Busy("contended") from None
        return "claimed", None

    def _finish(self, state_key: str, op: str, status: str, **fields: Any) -> dict[str, Any]:
        head = self.release_store.head(state_key)
        body = {"schema": layout.INTAKE_STATE_SCHEMA, "status": status, "operation_id": op, "machine": self.machine,
                "finished_at": _iso(self.now()), **fields}
        if head is None:
            self.release_store.put_bytes(state_key, _canonical(body), create_only=True, operation_id=op)
        else:
            self.release_store.put_bytes(state_key, _canonical(body), if_match=head.etag, operation_id=op)
        return body

    # -- tek paket -----------------------------------------------------------------------------------
    def process(self, package_id: str, announcement_etag: str, cancel: CancelToken | None = None) -> PackageOutcome:
        try:
            marker = validate_marker(_read_json(self.intake_store, layout.ready_key(package_id)), package_id)
        except NotFound:
            self._local(package_id, "WAITING_FOR_COMPLETION", "duyuru var, tamamlanma işareti yok", "INCOMPLETE",
                        announcement_etag=announcement_etag)
            return PackageOutcome(package_id, "SKIPPED", "INCOMPLETE")
        except Rejected as exc:
            self._local(package_id, "FAILED", exc.detail, exc.code, announcement_etag=announcement_etag, cloud_state="REJECTED")
            return PackageOutcome(package_id, "REJECTED", exc.code, exc.detail)
        sha = marker["manifest_sha256"]
        op = f"int-{sha[:24]}-{self.machine}"
        state_key = layout.intake_state_key(package_id, sha)
        base = {"dataset_id": marker.get("dataset_id"), "version": marker.get("version"), "fingerprint": sha,
                "announcement_etag": announcement_etag, "access": marker.get("access_class"), "bytes_total": marker["total_bytes"]}
        try:
            claim, terminal = self._claim(state_key, op)
        except Busy as exc:
            self._local(package_id, "VALIDATING", f"başka işlemci üzerinde: {exc}", "VALIDATING", **base)
            return PackageOutcome(package_id, "BUSY", "claimed_elsewhere")
        if terminal is not None:
            self._record_terminal(package_id, terminal, base)
            return PackageOutcome(package_id, terminal["status"], terminal.get("code", ""), terminal.get("detail", ""),
                                  terminal.get("dataset_id"), terminal.get("version"))
        work = self.staging_root / f"w-{hashlib.sha256(op.encode('utf-8')).hexdigest()[:12]}"  # kısa: Windows yol sınırı
        try:
            outcome = self._accept(package_id, marker, op, state_key, work, base, cancel)
        except Rejected as exc:
            state = self._finish(state_key, op, "REJECTED", code=exc.code, detail=exc.detail[:2000], package_id=package_id,
                                 dataset_id=marker.get("dataset_id"), version=marker.get("version"), manifest_sha256=sha,
                                 report=exc.report)
            self._record_terminal(package_id, state, base)
            shutil.rmtree(work, ignore_errors=True)
            return PackageOutcome(package_id, "REJECTED", exc.code, exc.detail, marker.get("dataset_id"), marker.get("version"))
        except Deferred as exc:
            self._local(package_id, "WAITING_FOR_COMPLETION", exc.detail, exc.code, **base)
            return PackageOutcome(package_id, "DEFERRED", exc.code, exc.detail)
        except TransientError as exc:
            self._local(package_id, "DETECTED", f"geçici ağ hatası; sonraki taramada sürer ({exc.code})", "cloud_offline", **base)
            return PackageOutcome(package_id, "FAILED_TRANSIENT", exc.code, exc.detail)
        except StorageError as exc:  # yetki/çakışma vb.: sonuçlanmamış sayılır, ayrıntı görünür; veri yazılmaz/silinmez
            self._local(package_id, "FAILED", f"{exc.code} {exc.detail}".strip(), "storage_error", **base)
            return PackageOutcome(package_id, "FAILED_TRANSIENT" if exc.retryable else "REJECTED", exc.code, exc.detail)
        return outcome

    def _record_terminal(self, package_id: str, state: dict[str, Any], base: dict[str, Any]) -> None:
        if state["status"] == "ACCEPTED":
            self._local(package_id, "REGISTERED", "", "", **base, cloud_state="ACCEPTED", storage="r2",
                        release_prefix=state.get("release_prefix"), quality=state.get("quality_status"))
        else:
            code = state.get("code") or "check_failed"
            target = "BLOCKED" if code in ("authorization_required", "VERSION_CONFLICT", "ALREADY_REGISTERED_LOCALLY",
                                           "version_conflict") or code.startswith("authorization") else "FAILED"
            self._local(package_id, target, state.get("detail", ""), code, **base, cloud_state="REJECTED")

    def _accept(self, package_id: str, marker: dict[str, Any], op: str, state_key: str, work: Path, base: dict[str, Any],
                cancel: CancelToken | None) -> PackageOutcome:
        from ..data.catalog_index import registered_fingerprint

        ds, ver, sha = marker.get("dataset_id"), marker.get("version"), marker["manifest_sha256"]
        try:
            catalog.version_dir(Path("."), ds, ver)
        except (ValueError, TypeError):
            raise Rejected("marker_invalid", "dataset_id_or_version") from None
        self._local(package_id, "VALIDATING", "işaret, yineleme ve yetki denetimi", "VALIDATING", **base)

        # Yineleme: bulutta yayımlanmış mı, yerelde kayıtlı mı (depolar arası tek kayıt).
        entry_key = layout.catalog_entry_key(ds, ver)
        if self.release_store.head(entry_key) is not None:
            entry = _read_json(self.release_store, entry_key)
            if entry.get("manifest_sha256") == sha:
                state = self._finish(state_key, op, "ACCEPTED", package_id=package_id, dataset_id=ds, version=ver,
                                     manifest_sha256=sha, release_prefix=entry.get("release_prefix"),
                                     quality_status=entry.get("quality_status"), note="already_published")
                self._record_terminal(package_id, state, base)
                return PackageOutcome(package_id, "ACCEPTED", "already_published", "", ds, ver)
            raise Rejected("VERSION_CONFLICT", f"{ds} {ver} bulutta farklı içerikle yayımlanmış; üzerine yazılmaz")
        local = catalog.version_dir(self.data_root, ds, ver)
        if (local / "version.json").is_file():
            same = registered_fingerprint(local) == sha
            raise Rejected("ALREADY_REGISTERED_LOCALLY" if same else "VERSION_CONFLICT",
                           f"{ds} {ver} yerelde kayıtlı; depolar arası yineleme yapılmaz (taşıma ayrı bir onaylı işlemdir)")

        access = str(marker.get("access_class") or "UNKNOWN")
        authorization: dict[str, Any] | None = None
        if access in auth.NEEDS_AUTHORIZATION:
            try:
                record = _read_json(self.intake_store, layout.authorization_key(package_id))
            except NotFound:
                raise Rejected("authorization_required", access) from None
            try:
                authorization = auth.validate(record, dataset_id=ds, version=ver, package_id=package_id, manifest_sha256=sha,
                                              access_class=access, provider=self.provider, bucket=self.bucket)
            except auth.AuthorizationError as exc:
                raise Rejected(exc.code, exc.detail) from None

        # Hazırlık alanı: indirilen paket + kayıt kopyası (en çok ~2x) ayrı sayılır; önbellek sınırına dahil değildir.
        total = int(marker["total_bytes"])
        need = 2 * total + STAGING_MARGIN
        self.staging_root.mkdir(parents=True, exist_ok=True)
        used = sum(p.stat().st_size for p in self.staging_root.rglob("*") if p.is_file() and not p.is_relative_to(work)) \
            if self.staging_root.exists() else 0
        if used + need > self.staging_max_bytes:
            raise Deferred("insufficient_staging_space", f"gerekli {need} B, hazırlık sınırı {self.staging_max_bytes} B, kullanımda {used} B")
        if self.disk_free(self.staging_root) < need:
            raise Deferred("insufficient_staging_space", f"diskte boş alan yetersiz: gerekli {need} B")

        pkg_dir = work / "p"
        reg_root = work / "r"
        done_marker = work / "registration.json"
        version_dir = catalog.version_dir(reg_root, ds, ver)
        # Windows yol sınırı: kayıt düzeni (data/datasets/<SEG>/<SEG>/<sürüm>/payload/...) uzundur. Aşılacaksa
        # paket reddedilmez; ERTELENİR ve daha kısa bir hazırlık klasörü önerilir.
        longest = max(len(str(version_dir / "payload" / Path(*f["path"].split("/")))) for f in marker["files"])
        if os.name == "nt" and longest > WINDOWS_PATH_LIMIT:
            raise Deferred("staging_path_too_long", f"en uzun yol {longest} karakter (> {WINDOWS_PATH_LIMIT}); "
                                                    f"cloud_intake.staging_dir için kısa bir klasör seçin (ör. C:\\TSTAGE)")
        entry: dict[str, Any] | None = None
        if done_marker.is_file() and (version_dir / "version.json").is_file():
            # Sürdürme: kayıt önceki denemede tamamlanmıştı. Yeniden kayıt yapılmaz (zaman damgalı kayıtlar değişir
            # ve kısmen yazılmış yayınla çakışırdı); aynı doğrulanmış kayıt dosyaları yayımlanır.
            entry = read_json(done_marker)
        else:
            self._download(package_id, marker, pkg_dir, base, cancel)
            manifest_sha, _ = sha256_path(pkg_dir / "PACKAGE" / "MANIFEST.json")
            if manifest_sha != sha:
                raise Rejected("sha256_mismatch", "PACKAGE/MANIFEST.json özeti işaretle uyuşmuyor")
            # Mevcut kayıt iş akışı (tüm kapılar ve QA) yalıtılmış geçici veri kökünde.
            self._local(package_id, "INGESTING", "bilimsel QA ve kayıt (mevcut iş akışı)", "INGESTING", **base)
            if reg_root.exists():
                _rmtree(reg_root)
            reg_root.mkdir(parents=True)
            from ..data.ingest import ingest_path

            entry = ingest_path(pkg_dir, reg_root)
            if entry.get("reason_code") == "unexpected":
                # Yerel ortam hatası (disk, yol, izin): paketin bilimsel reddi değildir → kalıcı RED yazılmaz.
                raise Deferred("unexpected", entry.get("reason") or "")
            if entry.get("state") != "REGISTERED":
                result = read_json(Path(entry["result_path"])) if entry.get("result_path") else {}
                raise Rejected(entry.get("reason_code") or "check_failed", entry.get("reason") or entry.get("state", ""),
                               report=_report_summary(result))
            write_json_atomic(done_marker, entry)
            _rmtree(pkg_dir)  # indirilen kopya artık gerekmiyor (kayıt kopyası doğrulandı): tek kopya kalsın
        result = read_json(Path(entry["result_path"])) if entry.get("result_path") else {}
        record = read_json(version_dir / "version.json")
        integrity = read_json(version_dir / "integrity.json")
        record_access = str(record.get("access", {}).get("classification") or "UNKNOWN")
        if record_access != access:
            raise Rejected("access_class_mismatch", f"işaret {access}, kayıt {record_access}")
        payload = version_dir / "payload"
        files = [ReleaseFile(f["path"], payload.joinpath(*f["path"].split("/")), f["sha256"], f["bytes"]) for f in integrity["files"]]
        for rel in UNHASHED_PACKAGE_FILES:
            p = payload.joinpath(*rel.split("/"))
            if p.is_file():
                digest, size = sha256_path(p)
                files.append(ReleaseFile(rel, p, digest, size))
        records = {n: (version_dir / n).read_bytes() for n in ("version.json", "quality.json", "integrity.json")}
        for report in version_dir.glob("*_DATA_INGESTION_REPORT.md"):
            records[report.name] = report.read_bytes()
        if entry.get("result_path"):
            records["INGESTION_RESULT.json"] = Path(entry["result_path"]).read_bytes()
        if authorization is not None:
            records[layout.AUTHORIZATION_NAME] = _canonical(authorization)

        self._local(package_id, "INGESTING", "yayın: yükleme + geri okuma doğrulaması + commit işareti", "PUBLISHING", **base)
        allowed = ("PUBLIC", "SYNTHETIC") + ((access,) if authorization is not None else ())
        commit = publish_release(self.release_store, dataset_id=ds, version=ver, access_class=access, files=files,
                                 records=records, operation_id=op,
                                 policy=PublicationPolicy(allow_cloud_publication=True, allowed_access_classes=allowed),
                                 cancel=cancel)
        from ..objectstore.publication import release_prefix

        prefix = release_prefix(ds, ver)
        catalog_entry = {"schema": layout.CATALOG_ENTRY_SCHEMA, "dataset_id": ds, "version": ver, "package_id": package_id,
                         "manifest_sha256": sha, "release_prefix": prefix, "operation_id": commit["operation_id"],
                         "committed_at": commit["committed_at"], "access_class": access,
                         "quality_status": read_json(version_dir / "quality.json").get("status"),
                         "object_count": len(commit["objects"]), "total_bytes": sum(o["size"] for o in commit["objects"])}
        try:
            self.release_store.put_bytes(entry_key, _canonical(catalog_entry), create_only=True, operation_id=op)
        except AlreadyExists:
            current = _read_json(self.release_store, entry_key)
            if current.get("operation_id") != commit["operation_id"]:
                raise Rejected("VERSION_CONFLICT", "katalog girdisi başka işlemle yazılmış") from None
        reconcile_dataset_pointer(self.release_store, ds, operation_id=op)
        state = self._finish(state_key, op, "ACCEPTED", package_id=package_id, dataset_id=ds, version=ver, manifest_sha256=sha,
                             release_prefix=prefix, quality_status=catalog_entry["quality_status"], access_class=access,
                             report=_report_summary(result))
        self._copy_run(entry)
        self._record_terminal(package_id, state, base)
        sync_catalog(self.release_store, self.data_root, provider=self.provider, bucket=self.bucket, namespace=self.namespace)
        _rmtree(work)  # yalnız başarılı yayın + doğrulama + katalog sonrası
        return PackageOutcome(package_id, "ACCEPTED", "", "", ds, ver)

    def _download(self, package_id: str, marker: dict[str, Any], pkg_dir: Path, base: dict[str, Any],
                  cancel: CancelToken | None) -> None:
        prefix = layout.package_prefix(package_id)
        done = 0
        total = int(marker["total_bytes"])
        for f in marker["files"]:
            dest = pkg_dir.joinpath(*f["path"].split("/"))
            if dest.is_file() and dest.stat().st_size == f["bytes"] and sha256_path(dest)[0] == f["sha256"]:
                done += f["bytes"]
                continue  # önceki (kesilmiş) denemede doğrulanmış
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".thermalis-part")
            digest = hashlib.sha256()
            size = 0
            try:
                with self.intake_store.open_read(prefix + f["path"]) as src, tmp.open("wb") as out:
                    for block in iter(lambda: src.read(4 * MIB), b""):
                        if cancel is not None:
                            cancel.raise_if_cancelled(prefix + f["path"])
                        digest.update(block)
                        size += len(block)
                        out.write(block)
            except NotFound:
                tmp.unlink(missing_ok=True)
                raise Rejected("package_object_missing", f["path"]) from None
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            if size != f["bytes"] or digest.hexdigest() != f["sha256"]:
                tmp.unlink(missing_ok=True)
                raise Rejected("sha256_mismatch", f["path"])
            tmp.replace(dest)
            done += size
            self._local(package_id, "INGESTING", "indirme ve SHA-256 doğrulaması", "DOWNLOADING", **{**base, "bytes_done": done,
                                                                                                    "bytes_total": total})

    def _copy_run(self, entry: dict[str, Any]) -> None:
        """Geçici kökteki çalışma raporu asıl veri kökünün çalışma geçmişine kopyalanır (yerel kabul geçmişi)."""
        if not entry.get("result_path"):
            return
        src = Path(entry["result_path"]).parent
        dest = catalog.data_dir(self.data_root) / "_ingest" / "runs" / src.name
        if not dest.exists():
            shutil.copytree(src, dest)


def _report_summary(result: dict[str, Any]) -> dict[str, Any]:
    stages = result.get("stages") or {}
    return {"status": result.get("status"), "stop_reason": result.get("stop_reason"),
            "stages": {k: {"status": v.get("status"), "detail": str(v.get("detail", ""))[:300]} for k, v in stages.items()}
            if isinstance(stages, dict) else None}


def _rmtree(path: Path) -> None:
    import os
    import stat

    for p in path.rglob("*") if path.exists() else []:
        if p.is_file():
            try:
                os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def cloud_intake_from_settings(settings, data_root: Path) -> CloudIntake:
    from ..objectstore.factory import cloud_store, staging_root
    from ..objectstore.prefixed import namespaced

    cloud = settings.cloud
    release = namespaced(cloud_store(settings, "publisher"), cloud.namespace)
    intake_bucket = cloud.intake_bucket or cloud.bucket
    intake = namespaced(cloud_store(settings, "publisher", bucket=intake_bucket), cloud.namespace) \
        if cloud.intake_bucket else release
    ci = settings.cloud_intake
    return CloudIntake(intake, release, data_root, provider=cloud.provider, bucket=cloud.bucket, intake_bucket=intake_bucket,
                       namespace=cloud.namespace, staging_root=staging_root(settings), staging_max_bytes=ci.staging_max_bytes,
                       claim_ttl_hours=ci.claim_ttl_hours, max_packages=ci.max_packages_per_scan)
