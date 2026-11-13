"""THERMALIS DATA otomatik entegrasyon iş akışı (``data ingest``).

Gelen paket klasöründeki (ayarlarda ``transfer_incoming_dir``) her alt klasörü
güvenilmeyen girdi olarak değerlendirir, hazır olanları mevcut kayıt mekanizmasıyla
(:func:`register.register_package`) kataloğa alır, sorgulanabilir dizini günceller ve
bağımsız denetimi çalıştırır. Kayıt mantığı yeniden yazılmaz; bu modül onun etrafındaki
keşif, kapı, kurtarma, denetim ve raporlama katmanıdır.

Durum eksenleri birbirinden ayrıdır:
- teknik entegrasyon: PASS | FAIL | BLOCKED | PENDING_VERIFICATION
- bilimsel kalite:    QA_PASS | QA_CONDITIONAL | QA_FAIL | QA_PENDING (paketten; asla yükseltilmez)
- bağımsız denetim:   NOT_CONFIGURED (bu sürümde yok; ``auditor`` uzantı noktası ileride doldurulur)
- erişim:             PUBLIC | RESTRICTED | UNKNOWN

Kaynak dosyalar hiçbir koşulda değiştirilmez, taşınmaz veya silinmez.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..storage import read_json, write_json_atomic
from . import catalog
from .adapters import classify_package, support_gap
from .catalog_index import dataset_profile, metadata_completeness, rebuild_index, registered_fingerprint
from .integrity import is_ephemeral, sha256_file
from .register import _make_writable, _move_dir, package_fingerprint, register_package
from .transfer_package import PackageError, access_class_from_release, load_package

TECH_PASS, TECH_FAIL, TECH_BLOCKED, TECH_PENDING = "PASS", "FAIL", "BLOCKED", "PENDING_VERIFICATION"
# Bağımsız denetim bu sürümde kapsam dışı. İleride eklenecek denetçi bu imzayla verilir ve sonucu S16'ya yazılır:
#   auditor(data_root, dataset_id, version, source_package) -> (durum, ayrıntı), durum ∈ AUDIT_STATUSES
Auditor = Callable[[Path, str, str, Path], tuple[str, str]]
AUDIT_NOT_CONFIGURED = "NOT_CONFIGURED"
AUDIT_STATUSES = ("VERIFIED", "FAILED", "PENDING")
NOT_RUN = "NOT_RUN"
ARCHIVE_SUFFIXES = (".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz")
PARTIAL_SUFFIXES = (".part", ".partial", ".tmp", ".crdownload", ".download", ".filepart", ".!ut")
INCOMPLETE_MARKERS = ("INCOMPLETE_DO_NOT_TRANSFER.txt", "COPYING_IN_PROGRESS", ".copying")
READY_MARKER = "THERMALIS_READY.json"
DEFAULT_SETTLE_SECONDS = 120
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

STAGES: tuple[tuple[str, str], ...] = (
    ("S01", "Paket keşfi ve güvenli okuma"),
    ("S02", "Manifesto ve paket yapısı"),
    ("S03", "SHA-256 bütünlüğü"),
    ("S04", "Dosya formatı ve okunabilirlik"),
    ("S05", "Veri kimliği ve mevcut sürüm"),
    ("S06", "Teknik metadata"),
    ("S07", "Mekânsal ve zamansal kapsam"),
    ("S08", "Şema ve anlamsal veri sözlüğü"),
    ("S09", "Kaynak, lisans ve erişim yetkisi"),
    ("S10", "Veri türüne özgü kalite kontrolü"),
    ("S11", "Kalite istisnalarının yapılandırılmış kaydı"),
    ("S12", "Kaynak ve türetilmiş veri ilişkileri"),
    ("S13", "Değişmez sürüm oluşturma"),
    ("S14", "Merkezi kataloğa kayıt"),
    ("S15", "Kayıt sonrası bütünlük ve sayım"),
    ("S16", "Bağımsız denetim"),
    ("S17", "Nihai kabul raporu"),
)
# Mevcut kayıt denetimlerinin aşamalara eşlenmesi.
CHECK_STAGE = {
    "PKG-01": "S02", "PKG-02": "S02", "PKG-03": "S02", "INT-01": "S03", "ENV-01": "S04", "VEC-00": "S04",
    "VEC-01": "S04", "CAT-01": "S05", "VEC-02": "S06", "VEC-03": "S06", "VEC-04": "S06", "VEC-06": "S06",
    "VEC-05": "S08", "DOM-01": "S08", "NUL-01": "S10", "QA-01": "S11", "INT-02": "S13", "CAT-02": "S13",
    "SYS-01": "S13", "INT-03": "S15", "VEC-07": "S15", "INT-04": "S15",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ingest_dir(data_root: Path) -> Path:
    return catalog.data_dir(data_root) / "_ingest"


def log_event(data_root: Path, **event: Any) -> None:
    """Yalnız eklenen denetim izi (JSON Lines)."""
    path = ingest_dir(data_root) / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"at": _utc_now(), **event}, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


# --------------------------------------------------------------------------- kilit

class IngestLocked(Exception):
    pass


class IngestLock:
    """Aynı anda tek entegrasyon. Çökme sonrası kalan kilit yalnız açık kullanıcı komutuyla kaldırılır."""

    def __init__(self, data_root: Path) -> None:
        self.path = ingest_dir(data_root) / "ingest.lock"

    def __enter__(self) -> "IngestLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            info = self.path.read_text(encoding="utf-8", errors="replace") if self.path.is_file() else ""
            raise IngestLocked(info) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "started_at": _utc_now()}, handle)
        return self

    def __exit__(self, *exc: Any) -> None:
        self.path.unlink(missing_ok=True)


def release_stale_lock(data_root: Path) -> bool:
    path = ingest_dir(data_root) / "ingest.lock"
    if path.is_file():
        log_event(data_root, event="stale_lock_released", detail=path.read_text(encoding="utf-8", errors="replace"))
        path.unlink()
        return True
    return False


# --------------------------------------------------------------------------- kurtarma

def recover(data_root: Path) -> list[dict[str, str]]:
    """Yarım kalmış işlemleri katalogdan çıkarır. Yalnız kilit tutulurken çağrılır.

    - ``_staging/*``: kilit bizdeyse başka çalışan iş yoktur → terk edilmiş; ``_failed`` altına taşınır.
    - ``REGISTERED`` olmayan sürüm klasörü (yayın sonrası çökme) → ``_failed`` altına taşınır.
    - ``dataset.json`` sürüm listesi kayıtlı klasörlerle uyumlu hâle getirilir.
    Hiçbir kaynak dosyaya dokunulmaz.
    """
    actions: list[dict[str, str]] = []
    data = catalog.data_dir(data_root)
    failed = data / "_failed"

    def quarantine(folder: Path, reason: str) -> None:
        failed.mkdir(parents=True, exist_ok=True)
        target = failed / f"{folder.name}-recovered-{secrets.token_hex(2)}"
        _make_writable(folder)
        _move_dir(folder, target)
        write_json_atomic(target / "RECOVERY.json", {"recovered_at": _utc_now(), "from": str(folder), "reason": reason})
        actions.append({"action": "quarantined", "from": str(folder), "to": str(target), "reason": reason})

    staging = data / "_staging"
    for folder in sorted(staging.iterdir()) if staging.is_dir() else []:
        if folder.is_dir():
            quarantine(folder, "abandoned_staging")
    datasets = data / "datasets"
    for record_path in sorted(datasets.rglob("version.json")) if datasets.is_dir() else []:
        try:
            status = read_json(record_path).get("registration", {}).get("status")
        except (OSError, ValueError):
            status = "UNREADABLE"
        if status != "REGISTERED":
            quarantine(record_path.parent, f"incomplete_publication:{status}")
    for dataset_file in sorted(datasets.rglob("dataset.json")) if datasets.is_dir() else []:
        d = read_json(dataset_file)
        registered = sorted(p.parent.name for p in dataset_file.parent.glob("*/version.json")
                            if read_json(p).get("registration", {}).get("status") == "REGISTERED")
        if sorted(d.get("versions", [])) != registered:
            d["versions"] = registered
            if d.get("latest_registered") not in registered:
                d["latest_registered"] = registered[-1] if registered else None
            write_json_atomic(dataset_file, d)
            actions.append({"action": "dataset_versions_reconciled", "from": str(dataset_file), "to": ",".join(registered), "reason": "catalog_bookkeeping"})
    for a in actions:
        log_event(data_root, event="recovery", **a)
    return actions


# --------------------------------------------------------------------------- keşif

@dataclass
class Candidate:
    path: Path
    state: str  # READY | NOT_A_PACKAGE | INCOMPLETE | SETTLING | ARCHIVE | UNSAFE | INVALID | ALREADY_REGISTERED | VERSION_CONFLICT
    reason: str = ""
    dataset_id: str | None = None
    version: str | None = None
    fingerprint: str | None = None
    signature: str | None = None  # dosya yolu+boyut+değişiklik zamanı özeti: kopyalama sürerken değişir
    ready_marker: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "state": self.state, "reason": self.reason, "dataset_id": self.dataset_id,
                "version": self.version, "fingerprint": self.fingerprint, "signature": self.signature,
                "ready_marker": self.ready_marker}


def _is_link(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _scan(root: Path) -> tuple[list[str], list[str], float, str]:
    """(bağlantılar, yarım dosyalar, en yeni değişiklik zamanı, imza). Bağlantıların içine girilmez.

    İmza, bütün dosyaların göreli yolu + boyutu + değişiklik zamanının SHA-256 özetidir;
    iki tarama arasında değişiyorsa kopyalama sürüyordur. İçerik burada okunmaz (büyük
    paketlerde ucuz kalır); içerik SHA-256'sı kayıt sırasında manifestle doğrulanır.
    """
    links: list[str] = []
    partial: list[str] = []
    entries: list[str] = []
    newest = os.lstat(root).st_mtime
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames.sort()
        for name in list(dirnames):
            p = base / name
            if _is_link(p):
                links.append(p.relative_to(root).as_posix())
                dirnames.remove(name)
            else:
                newest = max(newest, os.lstat(p).st_mtime)
        for name in sorted(filenames):
            p = base / name
            rel = p.relative_to(root).as_posix()
            if _is_link(p):
                links.append(rel)
                continue
            if name.lower().endswith(PARTIAL_SUFFIXES) or name.startswith("~$"):
                partial.append(rel)
            st = os.lstat(p)
            newest = max(newest, st.st_mtime)
            entries.append(f"{rel}|{st.st_size}|{st.st_mtime_ns}")
    signature = hashlib.sha256("\n".join(entries).encode("utf-8")).hexdigest()
    return links, partial, newest, signature


def read_ready_marker(folder: Path) -> tuple[bool, str | None]:
    """Hazır işareti (``THERMALIS_READY.json``): gönderen tarafın kopyalamayı bitirdiğini bildirir.

    İçeriği ``{"manifest_sha256": "<PACKAGE/MANIFEST.json SHA-256>"}`` olmalıdır. İşaret yoksa
    eski paketler gibi bekleme süresi + iki taramada değişmeme kuralı uygulanır.
    Döner: (işaret geçerli mi, hata nedeni | None).
    """
    marker = folder / READY_MARKER
    if not marker.is_file():
        return False, None
    try:
        declared = str(json.loads(marker.read_text(encoding="utf-8-sig")).get("manifest_sha256", "")).lower()
    except (OSError, ValueError, AttributeError):
        return False, "ready_marker_unreadable"
    manifest = folder / "PACKAGE" / "MANIFEST.json"
    if not manifest.is_file():
        return False, "ready_marker_without_manifest"
    if declared != sha256_file(manifest):
        return False, "ready_marker_manifest_mismatch"
    return True, None


def assess(folder: Path, data_root: Path, settle_seconds: float = DEFAULT_SETTLE_SECONDS, now: float | None = None) -> Candidate:
    """Bir klasörün işlenmeye hazır olup olmadığını manifest ve metadata üzerinden belirler (yalnız okur)."""
    now = time.time() if now is None else now
    if _is_link(folder):
        return Candidate(folder, "UNSAFE", "package_root_is_link")
    links, partial, newest, signature = _scan(folder)

    def candidate(state: str, reason: str = "", *args: Any, **kwargs: Any) -> Candidate:
        return Candidate(folder, state, reason, *args, signature=signature, **kwargs)

    if links:
        return candidate("UNSAFE", f"links_inside_package: {links[:5]}")
    markers = [m for m in INCOMPLETE_MARKERS if (folder / m).exists()]
    if markers:
        return candidate("INCOMPLETE", f"incomplete_marker: {markers}")
    if partial:
        return candidate("INCOMPLETE", f"partial_files: {partial[:5]}")
    if not (folder / "PACKAGE" / "MANIFEST.json").is_file() or not (folder / "PACKAGE" / "RELEASE_STATUS.json").is_file():
        return candidate("NOT_A_PACKAGE", "PACKAGE/MANIFEST.json veya PACKAGE/RELEASE_STATUS.json yok")
    marker_ok, marker_error = read_ready_marker(folder)
    if marker_error:
        return candidate("INVALID", marker_error)
    if settle_seconds > 0 and not marker_ok:
        age = now - newest
        if age < -5:
            return candidate("SETTLING", f"dosya zaman damgası {int(-age)} s gelecekte (saat farkı?); klasör durağan sayılamıyor")
        if age < settle_seconds:
            return candidate("SETTLING", f"son değişiklik {int(max(age, 0))} s önce (< {int(settle_seconds)} s)")
    try:
        pkg = load_package(folder)
    except PackageError as exc:
        return candidate("INVALID", f"{exc.code} {exc.detail}".strip())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return candidate("INVALID", f"package_unreadable: {type(exc).__name__}: {exc}")
    absent = [e.path for e in pkg.manifest if not (folder / e.path).is_file()]
    short = [e.path for e in pkg.manifest if (folder / e.path).is_file() and (folder / e.path).stat().st_size != e.bytes]
    declared = pkg.manifest_meta.get("file_count")
    if absent or short:
        return candidate("INCOMPLETE", f"manifest dosyaları eksik {len(absent)} / boyut farklı {len(short)} (kopyalama sürüyor olabilir)",
                         pkg.dataset_id, pkg.version, ready_marker=marker_ok)
    if isinstance(declared, int) and declared != len(pkg.manifest):
        return candidate("INVALID", f"manifest_file_count_mismatch: beyan {declared}, liste {len(pkg.manifest)}", pkg.dataset_id, pkg.version)
    fp = package_fingerprint(folder)
    existing = catalog.version_dir(data_root, pkg.dataset_id, pkg.version)
    if (existing / "version.json").is_file():
        if registered_fingerprint(existing) == fp:
            return candidate("ALREADY_REGISTERED", "aynı paket (MANIFEST SHA-256) zaten kayıtlı", pkg.dataset_id, pkg.version, fp,
                             ready_marker=marker_ok)
        return candidate("VERSION_CONFLICT", "aynı veri kimliği ve sürüm farklı içerikle kayıtlı; üzerine yazılmaz",
                         pkg.dataset_id, pkg.version, fp, ready_marker=marker_ok)
    return candidate("READY", "", pkg.dataset_id, pkg.version, fp, ready_marker=marker_ok)


def discover(incoming: Path, data_root: Path, settle_seconds: float = DEFAULT_SETTLE_SECONDS) -> list[Candidate]:
    out: list[Candidate] = []
    for entry in sorted(incoming.iterdir()):
        if entry.name.startswith("."):
            continue
        if _is_link(entry):
            out.append(Candidate(entry, "UNSAFE", "package_root_is_link"))
        elif entry.is_file():
            if entry.name.lower().endswith(ARCHIVE_SUFFIXES):
                st = os.lstat(entry)
                signature = hashlib.sha256(f"{entry.name}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8")).hexdigest()
                out.append(Candidate(entry, "ARCHIVE", "bağımsız arşiv: otomatik açılmaz, taşınmaz, silinmez; paket olarak "
                                                        "işlenmesi için bir klasöre çıkarılmalı", signature=signature))
        elif entry.is_dir():
            out.append(assess(entry, data_root, settle_seconds))
    return out


# --------------------------------------------------------------------------- tek paket

@dataclass
class Stage:
    id: str
    title: str
    status: str = NOT_RUN
    detail: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class IngestResult:
    item_id: str
    package_path: Path
    started_at: str
    stages: dict[str, Stage] = field(default_factory=lambda: {sid: Stage(sid, title) for sid, title in STAGES})
    technical: str = TECH_PENDING
    quality: str = "QA_PENDING"
    audit: str = AUDIT_NOT_CONFIGURED
    access: str = "UNKNOWN"
    dataset_id: str | None = None
    version: str | None = None
    package_id: str | None = None
    fingerprint: str | None = None
    stop_reason: str | None = None
    user_decision: str | None = None
    version_dir: Path | None = None
    register_report: Path | None = None
    format_plan: dict[str, Any] | None = None
    support_gap: dict[str, Any] | None = None
    completeness: list[dict[str, str]] = field(default_factory=list)
    findings: dict[str, Any] | None = None
    open_decisions: list[dict[str, Any]] = field(default_factory=list)
    source_snapshot_unchanged: bool | None = None
    prior_versions_unchanged: bool | None = None
    finished_at: str | None = None
    folder: Path | None = None

    def set(self, sid: str, status: str, detail: str = "", evidence: list[str] | None = None) -> None:
        stage = self.stages[sid]
        # Daha kötü durum, daha iyisinin üzerine yazar.
        order = {NOT_RUN: 0, "SKIPPED": 1, "PASS": 1, "WARN": 2, "PENDING": 3, "BLOCKED": 4, "FAIL": 5}
        if order.get(status, 0) >= order.get(stage.status, 0):
            stage.status = status
        if detail:
            stage.detail = f"{stage.detail} · {detail}" if stage.detail else detail
        stage.evidence += evidence or []

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0", "item_id": self.item_id, "package_name": self.package_path.name,
            "package_path": str(self.package_path), "package_id": self.package_id, "package_fingerprint": self.fingerprint,
            "dataset_id": self.dataset_id, "version": self.version, "started_at": self.started_at, "finished_at": self.finished_at,
            "status": {"technical": self.technical, "quality": self.quality, "independent_audit": self.audit, "access": self.access},
            "stop_reason": self.stop_reason, "user_decision_required": self.user_decision,
            "locations": {
                "source_package": str(self.package_path),
                "version_dir": str(self.version_dir) if self.version_dir else None,
                "ingestion_report": str(self.register_report) if self.register_report else None,
                "integrity_report": str(self.version_dir / "integrity.json") if self.version_dir else None,
                "quality_report": str(self.version_dir / "quality.json") if self.version_dir else None,
                "support_gap_report": str(self.folder / "SUPPORT_GAP.json") if self.support_gap and self.folder else None,
            },
            "format_plan": self.format_plan, "metadata_completeness": self.completeness, "quality_findings": self.findings,
            "open_scientific_decisions": self.open_decisions,
            "source_snapshot_unchanged": self.source_snapshot_unchanged, "prior_versions_unchanged": self.prior_versions_unchanged,
            "stages": [{"id": s.id, "title": s.title, "status": s.status, "detail": s.detail, "evidence": s.evidence} for s in self.stages.values()],
        }


def _snapshot_tree(root: Path) -> dict[str, tuple[int, int]]:
    return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


def _snapshot_versions(data_root: Path) -> dict[str, str]:
    datasets = catalog.data_dir(data_root) / "datasets"
    out = {}
    for record in sorted(datasets.rglob("version.json")) if datasets.is_dir() else []:
        for name in ("version.json", "quality.json", "integrity.json"):
            f = record.parent / name
            if f.is_file():
                out[f.relative_to(datasets).as_posix()] = sha256_file(f)
    return out


def _stop(result: IngestResult, sid: str, technical: str, reason: str, user_decision: str | None = None) -> IngestResult:
    result.set(sid, "BLOCKED" if technical == TECH_BLOCKED else "FAIL", reason)
    result.technical, result.stop_reason, result.user_decision = technical, reason, user_decision
    return result


def ingest_package(candidate: Candidate, data_root: Path, item_id: str, *, auditor: Auditor | None = None,
                   progress: Callable[[str], None] | None = None) -> IngestResult:
    result = IngestResult(item_id, candidate.path, _utc_now(), dataset_id=candidate.dataset_id,
                          version=candidate.version, fingerprint=candidate.fingerprint)
    if candidate.state != "READY":
        # UNSAFE → S01 BLOCKED (güvenlik); INVALID → S02 FAIL (paket yapısı); VERSION_CONFLICT → S05 BLOCKED (üzerine yazılmaz).
        sid, technical, decision = {
            "UNSAFE": ("S01", TECH_BLOCKED, "Pakette sembolik bağlantı/junction var; gerçek dosyalarla yeniden hazırlanmalı."),
            "INVALID": ("S02", TECH_FAIL, None),
            "VERSION_CONFLICT": ("S05", TECH_BLOCKED, "Aynı sürüm farklı içerikle gelemez; pakete yeni sürüm numarası verilmeli."),
        }.get(candidate.state, ("S01", TECH_FAIL, None))
        return _stop(result, sid, technical, f"{candidate.state}: {candidate.reason}", decision)
    result.set("S01", "PASS", "hazır: bağlantı yok, yarım dosya yok, manifest dosyaları tam, klasör durağan")

    pkg = load_package(candidate.path)
    result.package_id = pkg.package_id
    result.quality = pkg.quality_status
    result.access = access_class_from_release(pkg.release_status)
    result.set("S02", "PASS", f"{len(pkg.manifest)} manifest dosyası · paket {pkg.package_id}")

    plan = classify_package(pkg)
    result.format_plan = plan.as_dict()
    if plan.unsupported or plan.declared_kinds or not plan.sources:
        result.support_gap = support_gap(pkg, plan)
        suffixes = ", ".join(f"{s} ({v['files']})" for s, v in result.support_gap["unsupported_by_suffix"].items())
        detail = suffixes or ", ".join(plan.declared_kinds) or "pakette tanınan veri kaynağı yok"
        return _stop(result, "S04", TECH_BLOCKED, f"format_not_supported: {detail}",
                     "Bu veri türü için adaptör yok. Paket işlenmedi; SUPPORT_GAP.json'a göre adaptör geliştirilmeli.")
    result.set("S04", "PASS", "adaptörler: " + ", ".join(f"{k}={len(v)}" for k, v in plan.sources.items()))

    # S09 — yetki: kararı otomasyon vermez.
    rs = pkg.release_status
    authorization = str(rs.get("transfer_authorization") or "").strip()
    license_docs = [e.path for e in pkg.manifest if "LICENSE" in e.path.upper()]
    if result.access == "UNKNOWN":
        return _stop(result, "S09", TECH_BLOCKED, "access_classification_unknown: paket erişim sınıfını beyan etmiyor",
                     "Erişim sınıfı (PUBLIC/RESTRICTED) veri sahibi tarafından beyan edilmeli.")
    if result.access == "RESTRICTED" and not authorization:
        return _stop(result, "S09", TECH_BLOCKED, "authorization_statement_missing: kısıtlı veri için aktarım/kullanım yetkisi beyanı yok",
                     "Veri sahibinin kullanım yetkisi belgelenmeli.")
    result.set("S09", "PASS" if license_docs else "WARN",
               f"erişim {result.access} · yetki: {authorization or '—'} · lisans belgesi: {', '.join(license_docs) or 'yok'}")

    source_before = _snapshot_tree(candidate.path)
    versions_before = _snapshot_versions(data_root)
    if progress:
        progress("INGESTING")
    log_event(data_root, event="register_start", item_id=item_id, package=str(candidate.path), dataset_id=pkg.dataset_id, version=pkg.version)
    outcome = register_package(candidate.path, data_root)
    log_event(data_root, event="register_end", item_id=item_id, status=outcome.status, ingest_id=outcome.ingest_id, error=outcome.error)
    result.register_report = outcome.report_path
    for check in outcome.checks:
        sid = CHECK_STAGE.get(check.id, "S13")
        status = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN", "INFO": "PASS"}[check.status]
        result.set(sid, status, evidence=[f"{check.id} {check.status}: {check.title} — {check.detail}"])

    result.source_snapshot_unchanged = _snapshot_tree(candidate.path) == source_before
    versions_after = _snapshot_versions(data_root)
    result.prior_versions_unchanged = all(versions_after.get(k) == v for k, v in versions_before.items())
    if not result.source_snapshot_unchanged:
        result.set("S15", "FAIL", "kaynak paket kayıt sırasında değişti")
    if not result.prior_versions_unchanged:
        result.set("S15", "FAIL", "önceki sürüm kayıtları değişti")

    if outcome.status != "PASS":
        result.technical, result.stop_reason = TECH_FAIL, outcome.error
        return result

    result.version_dir = outcome.location
    record = read_json(outcome.location / "version.json")
    quality = read_json(outcome.location / "quality.json")
    result.quality = quality["status"]

    # S07 kapsam, S08 anlamsal sözlük, S11 bulgu sayımı: kayıttan okunur, uydurulmaz.
    extents = {n: l.get("extent_native") for n, l in record["layers"].items() if l.get("role") == "core"}
    profile = dataset_profile(outcome.location, record)
    result.completeness = [{"item": i, "state": s, "detail": d} for i, s, d in metadata_completeness(record, quality, profile)]
    temporal = next(c for c in result.completeness if c["item"] == "temporal_coverage")
    result.set("S07", "PASS" if all(extents.values()) else "WARN",
               f"mekânsal kapsam (yerel CRS {record['crs'].get('native_crs_name')}): {len([e for e in extents.values() if e])}/{len(extents)} çekirdek katman"
               f" · zamansal kapsam: {temporal['state']}")
    semantic = next(c for c in result.completeness if c["item"] == "semantic_definitions")
    result.set("S08", "PASS" if semantic["state"] == "present" else "WARN", f"anlamsal tanımlar: {semantic['state']} ({semantic['detail']})")
    exc = quality["exceptions"]
    result.findings = {"records": len(exc), "unique_ids": len({e["exception_id"] for e in exc}),
                       "by_origin": quality.get("summary_by_origin"), "by_category": quality.get("summary"),
                       "layer_status": len(quality.get("layer_status", []))}
    result.open_decisions = [{"gate": g.get("id"), "status": g.get("status"), "detail": g.get("detail")}
                             for g in quality.get("gates", []) if g.get("status") not in ("PASS",)]
    result.open_decisions += [{"finding": e["exception_id"], "disposition": e.get("disposition")}
                              for e in exc if str(e.get("disposition") or "").startswith("OPEN")]
    result.set("S11", "PASS", f"{len(exc)} kayıt · {result.findings['unique_ids']} tekil kimlik · köken {quality.get('summary_by_origin')}")

    index_ok = True
    try:
        index = rebuild_index(data_root)
        result.set("S12", "PASS", "ilişkiler dizine yazıldı (sürüm↔paket, katman↔kapsayıcı, bulgu↔kanıt)", [str(index)])
        result.set("S14", "PASS", f"katalog dizini güncellendi: {index.name}", [str(index)])
    except Exception as exc_:  # dizin türetilmiş veridir; kayıt geçerli kalır ama tutarlılık doğrulanmadı
        index_ok = False
        result.set("S14", "PENDING", f"catalog_index_rebuild_failed: {exc_}")
        result.stop_reason = f"catalog_index_rebuild_failed: {exc_}"

    if result.stages["S15"].status == "FAIL":
        result.technical = TECH_FAIL
    elif not index_ok:
        result.technical = TECH_PENDING
    else:
        result.technical = TECH_PASS

    if auditor is None:
        result.set("S16", "SKIPPED", "not_configured: bu sürümde bağımsız denetim yok; geçerli doğrulama S03/S13/S15 teknik kontrolleridir")
    else:
        status, detail = auditor(data_root, pkg.dataset_id, pkg.version, candidate.path)
        if status not in AUDIT_STATUSES:
            status, detail = "PENDING", f"geçersiz denetçi durumu: {status}"
        result.audit = status
        result.set("S16", {"VERIFIED": "PASS", "FAILED": "FAIL"}.get(status, "PENDING"), f"{status}: {detail}")
    return result


# --------------------------------------------------------------------------- rapor

def render_run_report(result: IngestResult) -> str:
    d = result.as_dict()
    s = d["status"]
    lines = [f"# Entegrasyon sonucu — `{result.package_path.name}`", "",
             "| Eksen | Durum |", "|---|---|",
             f"| Teknik entegrasyon | **{s['technical']}** |", f"| Bilimsel kalite | **{s['quality']}** (paketten; yükseltilmez) |",
             f"| Bağımsız denetim | **{s['independent_audit']}** |", f"| Erişim | **{s['access']}** |", "",
             "Kaydın başarılı olması bilimsel uygunluk veya yayımlama yetkisi anlamına gelmez.", ""]
    if result.stop_reason:
        lines += [f"- **Durma nedeni:** `{result.stop_reason}`"]
    if result.user_decision:
        lines += [f"- **Kullanıcı kararı gerekli:** {result.user_decision}"]
    lines += ["", "## Kimlik ve konumlar", "", "| Alan | Değer |", "|---|---|",
              f"| Veri kimliği | `{result.dataset_id or '—'}` |", f"| Sürüm | `{result.version or '—'}` |",
              f"| Paket | `{result.package_id or '—'}` |", f"| Paket parmak izi (MANIFEST SHA-256) | `{result.fingerprint or '—'}` |"]
    for key, value in d["locations"].items():
        lines.append(f"| {key} | `{value or '—'}` |")
    lines += ["", "## Aşamalar", "", "| Aşama | Başlık | Durum | Ayrıntı |", "|---|---|---|---|"]
    for st in result.stages.values():
        lines.append(f"| {st.id} | {st.title} | {st.status} | {st.detail.replace('|', '/') or '—'} |")
    if result.completeness:
        lines += ["", "## Metadata tamlığı", "", "| Öğe | Durum | Ayrıntı |", "|---|---|---|"]
        lines += [f"| {c['item']} | {c['state']} | {c['detail'].replace('|', '/')} |" for c in result.completeness]
    if result.findings:
        f = result.findings
        lines += ["", "## Kalite kayıtları", "", f"- {f['records']} kayıt · {f['unique_ids']} tekil kimlik · köken {f['by_origin']}",
                  f"- Katman durumu notu (bulgu değil): {f['layer_status']}"]
    if result.open_decisions:
        lines += ["", f"## Açık bilimsel kararlar ({len(result.open_decisions)})", ""]
        lines += [f"- {json.dumps(o, ensure_ascii=False)}" for o in result.open_decisions[:60]]
    lines += ["", "## Koruma", "", f"- Kaynak paket değişmedi: {result.source_snapshot_unchanged}",
              f"- Önceki sürüm kayıtları değişmedi: {result.prior_versions_unchanged}", ""]
    return "\n".join(lines)


def _write_result(data_root: Path, result: IngestResult) -> None:
    result.finished_at = _utc_now()
    folder = ingest_dir(data_root) / "runs" / result.item_id
    folder.mkdir(parents=True, exist_ok=True)
    result.folder = folder
    written = ["INGESTION_RESULT.json", "INGESTION_REPORT.md"] + (["SUPPORT_GAP.json"] if result.support_gap else [])
    result.set("S17", "PASS", " + ".join(written) + " yazıldı", [str(folder)])
    if result.support_gap:
        write_json_atomic(folder / "SUPPORT_GAP.json", result.support_gap)
    (folder / "INGESTION_REPORT.md").write_text(render_run_report(result), encoding="utf-8", newline="\n")
    write_json_atomic(folder / "INGESTION_RESULT.json", result.as_dict())


# --------------------------------------------------------------------------- giriş noktası

@dataclass
class RunSummary:
    run_id: str
    candidates: list[Candidate]
    results: list[IngestResult]
    recovery: list[dict[str, str]]

    @property
    def ok(self) -> bool:
        return all(r.technical == TECH_PASS for r in self.results)


PROCESS_STATES = ("VERSION_CONFLICT", "UNSAFE", "INVALID")  # READY dışında sonuç raporu üretilen durumlar
# SETTLING / INCOMPLETE / NOT_A_PACKAGE / ARCHIVE / ALREADY_REGISTERED: işlenmez, sonraki çalıştırmada yeniden değerlendirilir.


# --------------------------------------------------------------------------- paket kabul durumu (kalıcı)
#
# Her paket için tek bir işlem durumu tutulur (``_ingest/intake.json``). Komut satırı, arka plan
# izleyicisi ve arayüzden elle ekleme aynı durumu günceller; uygulama yeniden başlasa da tutarlı kalır.
# Bu teknik kabul durumudur; bilimsel kalite (QA_*) ayrı eksendir ve buraya karışmaz.

INTAKE_STATES = ("DETECTED", "WAITING_FOR_COMPLETION", "VALIDATING", "READY", "INGESTING", "REGISTERED",
                 "ALREADY_REGISTERED", "BLOCKED", "ADAPTER_REQUIRED", "FAILED", "UNSUPPORTED_INPUT")
# UNSUPPORTED_INPUT: gelen klasörde paket olarak teslim edilmemiş girdi (ör. bağımsız .zip arşivi).
# Bir entegrasyon hatası değildir; açılmaz, taşınmaz, silinmez ve başarısız sayılmaz.
INTAKE_SCHEMA_VERSION = "1.1"
# Arayüzün çevirdiği neden kodları (frontend locale'lerinde data.intake.reason.<kod>; test kontrol eder).
REASON_CODES = (
    "ALREADY_REGISTERED", "ARCHIVE", "SETTLING", "INCOMPLETE", "NOT_A_PACKAGE", "STABILITY_CHECK", "UNSAFE", "INVALID",
    "VERSION_CONFLICT", "INTERRUPTED", "RETRY_REQUESTED", "VALIDATING", "INGESTING", "format_not_supported",
    "authorization_statement_missing", "access_classification_unknown", "check_failed", "unexpected",
)
RETRYABLE_STATES = ("BLOCKED", "ADAPTER_REQUIRED", "FAILED")
IN_FLIGHT_STATES = ("VALIDATING", "INGESTING")
CANDIDATE_TO_INTAKE = {
    "READY": "READY", "SETTLING": "WAITING_FOR_COMPLETION", "INCOMPLETE": "WAITING_FOR_COMPLETION",
    "NOT_A_PACKAGE": "WAITING_FOR_COMPLETION", "ARCHIVE": "UNSUPPORTED_INPUT", "UNSAFE": "BLOCKED", "VERSION_CONFLICT": "BLOCKED",
    "INVALID": "FAILED", "ALREADY_REGISTERED": "ALREADY_REGISTERED",
}
# Aynı süreçteki iş parçacıkları (izleyici + elle ekleme) sırayla çalışır; süreçler arası IngestLock dosyasıdır.
_PROCESS_LOCK = threading.Lock()


def intake_path(data_root: Path) -> Path:
    return ingest_dir(data_root) / "intake.json"


def _migrate_intake(intake: dict[str, Any]) -> dict[str, Any]:
    """1.0 → 1.1: bağımsız arşivler BLOCKED değil UNSUPPORTED_INPUT olarak sınıflanır.

    Yalnız hiç işlenmemiş (``item_id`` yok) ve neden kodu ARCHIVE olan kayıtlar uyarlanır; geçmiş
    korunur ve yeniden sınıflama geçmişe eklenir. Gerçek BLOCKED/FAILED kayıtlarına dokunulmaz.
    """
    if intake.get("schema_version") == INTAKE_SCHEMA_VERSION:
        return intake
    for key, entry in intake["packages"].items():
        if entry.get("state") == "BLOCKED" and entry.get("reason_code") == "ARCHIVE" and not entry.get("item_id"):
            entry["history"] = (entry.get("history", []) + [{
                "at": _utc_now(), "state": "UNSUPPORTED_INPUT", "reason_code": "ARCHIVE",
                "reason": "yeniden sınıflandı: BLOCKED → UNSUPPORTED_INPUT (bağımsız arşiv bir entegrasyon hatası değildir)"}])[-20:]
            entry.update(state="UNSUPPORTED_INPUT", reclassified_from="BLOCKED", state_since=_utc_now())
    intake["schema_version"] = INTAKE_SCHEMA_VERSION
    return intake


def load_intake(data_root: Path) -> dict[str, Any]:
    path = intake_path(data_root)
    if path.is_file():
        try:
            data = read_json(path)
            if isinstance(data, dict) and isinstance(data.get("packages"), dict):
                return _migrate_intake(data)
        except (OSError, ValueError):
            pass
    return {"schema_version": INTAKE_SCHEMA_VERSION, "packages": {}}


def _save_intake(data_root: Path, intake: dict[str, Any]) -> None:
    intake["schema_version"] = INTAKE_SCHEMA_VERSION
    intake["updated_at"] = _utc_now()
    write_json_atomic(intake_path(data_root), intake)


def _set_state(intake: dict[str, Any], path: Path, state: str, reason: str = "", reason_code: str = "", **extra: Any) -> dict[str, Any]:
    """``reason_code`` dilden bağımsız neden kodudur (arayüz çevirir); ``reason`` ayrıntı/kanıt metnidir."""
    key = str(path)
    now = _utc_now()
    entry = intake["packages"].get(key) or {"name": path.name, "path": key, "first_seen": now, "history": []}
    if entry.get("state") != state:
        entry["history"] = (entry.get("history", []) + [{"at": now, "state": state, "reason_code": reason_code, "reason": reason}])[-20:]
        entry["state_since"] = now
    entry.update({"state": state, "reason": reason, "reason_code": reason_code, "updated_at": now, "present": True, **extra})
    intake["packages"][key] = entry
    return entry


def _result_code(result: IngestResult) -> str:
    if result.technical == TECH_PASS:
        return ""
    prefix = (result.stop_reason or "").split(":", 1)[0].strip()
    if re.fullmatch(r"[A-Z]{3}-\d{2}", prefix):
        return "check_failed"
    if prefix.startswith("UNEXPECTED") or prefix.startswith("catalog_index"):
        return "unexpected"
    return prefix


def _result_state(result: IngestResult) -> str:
    if result.technical in (TECH_PASS, TECH_PENDING):
        return "REGISTERED"
    if result.technical == TECH_BLOCKED:
        return "ADAPTER_REQUIRED" if (result.stop_reason or "").startswith("format_not_supported") else "BLOCKED"
    return "FAILED"


def _process(candidate: Candidate, data_root: Path, item_id: str, intake: dict[str, Any], auditor: Auditor | None) -> IngestResult:
    base = {"dataset_id": candidate.dataset_id, "version": candidate.version, "fingerprint": candidate.fingerprint,
            "signature": candidate.signature, "ready_marker": candidate.ready_marker}
    _set_state(intake, candidate.path, "VALIDATING", "manifest, format ve yetki kapıları", "VALIDATING", **base)
    _save_intake(data_root, intake)

    def progress(state: str) -> None:
        _set_state(intake, candidate.path, state, "SHA-256 doğrulaması, değişmez sürüm, katalog kaydı", "INGESTING", **base)
        _save_intake(data_root, intake)

    try:
        result = ingest_package(candidate, data_root, item_id, auditor=auditor, progress=progress)
    except Exception as exc:  # beklenmeyen hata: kontrollü dur, yarım kayıt bırakma
        result = IngestResult(item_id, candidate.path, _utc_now())
        _stop(result, "S13", TECH_FAIL, f"UNEXPECTED: {type(exc).__name__}: {exc}")
        recover(data_root)
    _write_result(data_root, result)
    _set_state(intake, candidate.path, _result_state(result), result.stop_reason or "", _result_code(result), **base,
               item_id=item_id, technical=result.technical, quality=result.quality, access=result.access,
               result_path=str(result.folder / "INGESTION_RESULT.json") if result.folder else None,
               support_gap_path=str(result.folder / "SUPPORT_GAP.json") if result.support_gap and result.folder else None,
               user_decision=result.user_decision)
    _save_intake(data_root, intake)
    log_event(data_root, event="item_done", item_id=item_id, technical=result.technical, quality=result.quality,
              audit=result.audit, access=result.access, stop_reason=result.stop_reason)
    return result


def run_ingest(incoming: Path, data_root: Path, *, settle_seconds: float = DEFAULT_SETTLE_SECONDS,
               auditor: Auditor | None = None, dry_run: bool = False, only: str | None = None,
               require_stable: bool = False, retry_failed: bool = True, wait: bool = True) -> RunSummary:
    """Gelen klasörü tarar, durumları kaydeder, hazır paketleri sırayla işler.

    ``require_stable``: hazır işareti olmayan paket, imzası bir önceki taramayla aynı olmadıkça
    işlenmez (izleyici kullanır: kopyalama sırasında değişen dosya yakalanır).
    ``retry_failed``: imzası değişmemiş BLOCKED/FAILED/ADAPTER_REQUIRED paket yeniden denenir mi
    (komut satırı: evet; izleyici: hayır — aynı hatayı her taramada tekrarlamaz).
    ``wait``: aynı süreçte başka iş sürüyorsa beklemek yerine ``IngestLocked`` (izleyici: bekleme).
    """
    incoming = Path(incoming)
    if not incoming.is_dir():
        raise FileNotFoundError(f"incoming folder not found: {incoming}")
    run_id = f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
    if not _PROCESS_LOCK.acquire(blocking=wait):
        raise IngestLocked("in-process ingest is running")
    try:
        with IngestLock(data_root):
            recovery = [] if dry_run else recover(data_root)
            candidates = discover(incoming, data_root, settle_seconds)
            if only:
                candidates = [c for c in candidates if c.path.name == only]
            log_event(data_root, event="discovery", run_id=run_id, dry_run=dry_run, candidates=[c.as_dict() for c in candidates])
            results: list[IngestResult] = []
            if dry_run:
                return RunSummary(run_id, candidates, results, recovery)

            intake = load_intake(data_root)
            for key, entry in list(intake["packages"].items()):  # çökme sonrası yarım kalan işlem
                if entry.get("state") in IN_FLIGHT_STATES:
                    _set_state(intake, Path(key), "DETECTED", f"önceki işlem yarıda kaldı ({entry.get('state')}); yeniden değerlendirilecek", "INTERRUPTED")
                    intake["packages"][key]["signature"] = None
            seen = {str(c.path) for c in candidates}
            for key, entry in intake["packages"].items():
                if Path(key).parent == incoming and key not in seen:
                    entry["present"] = False
            to_process: list[Candidate] = []
            for c in candidates:
                prev = intake["packages"].get(str(c.path))
                state = CANDIDATE_TO_INTAKE[c.state]
                same = prev is not None and prev.get("signature") == c.signature
                extra = {"dataset_id": c.dataset_id, "version": c.version, "fingerprint": c.fingerprint,
                         "signature": c.signature, "ready_marker": c.ready_marker}
                if state == "READY":
                    if same and prev.get("state") in RETRYABLE_STATES and not retry_failed:
                        continue  # değişmemiş başarısız paket: aynı hatayı tekrar üretme
                    if require_stable and not c.ready_marker and not (same and prev.get("state") in ("WAITING_FOR_COMPLETION", "DETECTED", "READY")):
                        _set_state(intake, c.path, "WAITING_FOR_COMPLETION",
                                   "kararlılık kontrolü: dosyalar bir sonraki taramada değişmemiş olmalı", "STABILITY_CHECK", **extra)
                        continue
                    _set_state(intake, c.path, "READY", "", **extra)
                    to_process.append(c)
                elif state == "ALREADY_REGISTERED" and prev and prev.get("state") == "REGISTERED":
                    prev.update(present=True, updated_at=_utc_now())  # kendi kaydettiğimiz paket: REGISTERED kalır
                elif state in RETRYABLE_STATES and same and prev.get("state") == state and not retry_failed:
                    prev.update(present=True, reason_code=prev.get("reason_code") or c.state)
                elif state == "UNSUPPORTED_INPUT" and same and prev.get("state") == state:
                    prev["present"] = True  # değişmemiş bağımsız girdi: her taramada yeni kayıt/uyarı üretilmez
                elif c.state in PROCESS_STATES:
                    to_process.append(c)  # güvenlik/yapı/sürüm sorunu: sonuç raporuyla birlikte kaydedilir
                else:
                    _set_state(intake, c.path, state, c.reason, c.state, **extra)
            _save_intake(data_root, intake)
            for n, candidate in enumerate(to_process):
                results.append(_process(candidate, data_root, f"{run_id}-{n + 1:02d}", intake, auditor))
            if results:
                try:
                    rebuild_index(data_root)
                except Exception as exc:  # pragma: no cover - dizin türetilmiş; kayıtlar geçerli
                    log_event(data_root, event="index_rebuild_failed", run_id=run_id, error=str(exc))
    finally:
        _PROCESS_LOCK.release()
    return RunSummary(run_id, candidates, results, recovery)


class PackageMismatch(Exception):
    pass


def ingest_path(package: Path, data_root: Path, *, expect_dataset_id: str | None = None,
                auditor: Auditor | None = None) -> dict[str, Any]:
    """Elle ekleme / yeni sürüm yükleme: tek bir paket klasörünü aynı iş akışından geçirir.

    Kullanıcı paketin hazır olduğunu söylemiştir, bu yüzden bekleme süresi uygulanmaz; manifest,
    dosya boyutları ve SHA-256 yine doğrulanır. ``expect_dataset_id`` verilirse ("yeni sürüm yükle")
    paket başka bir veri setine aitse hiçbir şey yapılmaz.
    """
    package = Path(package)
    with _PROCESS_LOCK, IngestLock(data_root):
        recover(data_root)
        candidate = assess(package, data_root, settle_seconds=0)
        if expect_dataset_id and candidate.dataset_id and candidate.dataset_id != expect_dataset_id:
            raise PackageMismatch(f"{candidate.dataset_id} != {expect_dataset_id}")
        intake = load_intake(data_root)
        extra = {"dataset_id": candidate.dataset_id, "version": candidate.version, "fingerprint": candidate.fingerprint,
                 "signature": candidate.signature, "ready_marker": candidate.ready_marker, "source": "manual"}
        log_event(data_root, event="manual_submit", package=str(package), state=candidate.state)
        if candidate.state != "READY":
            entry = _set_state(intake, package, CANDIDATE_TO_INTAKE[candidate.state], candidate.reason, candidate.state, **extra)
            _save_intake(data_root, intake)
            return entry
        _set_state(intake, package, "READY", "", **extra)
        item_id = f"man-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
        _process(candidate, data_root, item_id, intake, auditor)
        try:
            rebuild_index(data_root)
        except Exception as exc:  # pragma: no cover
            log_event(data_root, event="index_rebuild_failed", item_id=item_id, error=str(exc))
        return intake["packages"][str(package)]


def forget_failure(data_root: Path, key: str) -> bool:
    """Arayüzden "yeniden dene": başarısız paketin imzasını unutur; izleyici sonraki taramada yeniden dener."""
    with _PROCESS_LOCK:
        intake = load_intake(data_root)
        entry = intake["packages"].get(key)
        if not entry or entry.get("state") not in RETRYABLE_STATES:
            return False
        entry.update(state="DETECTED", reason="kullanıcı yeniden deneme istedi", reason_code="RETRY_REQUESTED",
                     signature=None, updated_at=_utc_now())
        _save_intake(data_root, intake)
        return True
