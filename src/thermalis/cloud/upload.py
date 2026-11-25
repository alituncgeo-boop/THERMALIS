"""THERMALIS paket yükleyici — ikinci bilgisayar için bağımsız komut (tam uygulama gerekmez).

Yalnız ``upload`` pixi ortamı yeterlidir (Python + boto3 + keyring; GDAL/Node/FastAPI yok).
Geliştirme bilgisayarının açık olması gerekmez: paket doğrudan R2'nin ``incoming/`` önekine yüklenir.

Adımlar (kaynak dosyalar yalnız OKUNUR, hiçbir zaman değiştirilmez):
1. Yapı: ``PACKAGE/MANIFEST.json``, ``RELEASE_STATUS.json``, envanter, sözlük, domainler, CRS; veri kimliği ve sürüm.
2. Güvenlik: bağlantı (symlink/junction) yok, yarım-kopya işareti yok, ayrılmış adlar yok.
3. SHA-256: manifestteki her dosya baştan sona okunur ve doğrulanır.
4. Yetki: RESTRICTED/UNKNOWN paket yalnız geçerli ``--authorization`` kaydıyla yüklenir (sınıf değişmez).
5. Yinelenme: aynı paket zaten yüklendiyse işlem yapılmaz; aynı kimlikle farklı içerik reddedilir.
6. Yükleme: yalnız-oluştur, büyük dosyalar çok parçalı; durum diskte tutulur, aynı komutla kaldığı yerden sürer.
7. Denetim: her nesnenin boyutu ve kayıtlı SHA-256'sı kontrol edilir (metadata düzeyi).
8. ``THERMALIS_READY.json`` EN SON yazılır; ardından keşif duyurusu ``incoming/_ready/<paket>.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TextIO

from ..data.integrity import is_ephemeral, verify_tree
from ..data.transfer_package import UNHASHED_PACKAGE_FILES, PackageError, access_class_from_release, load_package
from ..objectstore.base import CancelToken, ObjectStore, sha256_path
from ..objectstore.errors import AlreadyExists, CredentialsMissing, StorageError, TransientError
from . import authorization as auth
from . import layout

TOOL = "thermalis-upload/1"
INCOMPLETE_MARKERS = ("INCOMPLETE_DO_NOT_TRANSFER.txt", "COPYING_IN_PROGRESS", ".copying")
PARTIAL_SUFFIXES = (".part", ".partial", ".tmp", ".crdownload", ".download", ".filepart", ".!ut")
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class UploadRejected(Exception):
    """Paket bu hâliyle yüklenemez (kullanıcı düzeltmeli). Ağ hatası değildir."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class PlannedFile:
    relpath: str
    path: Path
    size: int
    sha256: str
    mtime_ns: int


@dataclass
class UploadPlan:
    root: Path
    package_id: str
    dataset_id: str
    version: str
    manifest_sha256: str
    access_class: str
    quality_status: str
    files: list[PlannedFile] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)


def _is_link(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def plan_package(root: Path) -> UploadPlan:
    """Ağ kullanmadan paketin yüklenebilirliğini doğrular ve yükleme planını üretir (yalnız okuma)."""
    root = Path(root)
    if not root.is_dir():
        raise UploadRejected("package_not_found", str(root))
    if _is_link(root):
        raise UploadRejected("package_root_is_link")
    for marker in INCOMPLETE_MARKERS:
        if (root / marker).exists():
            raise UploadRejected("package_incomplete_marker", marker)
    for path in root.rglob("*"):
        if _is_link(path):
            raise UploadRejected("package_contains_link", path.relative_to(root).as_posix())
        if path.is_file() and path.name.lower().endswith(PARTIAL_SUFFIXES):
            raise UploadRejected("package_contains_partial_file", path.relative_to(root).as_posix())
    try:
        pkg = load_package(root)
    except PackageError as exc:
        raise UploadRejected(exc.code, exc.detail) from None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UploadRejected("package_unreadable", type(exc).__name__) from None
    try:
        layout.validate_package_id(pkg.package_id)
    except ValueError:
        raise UploadRejected("invalid_package_id", pkg.package_id) from None
    if any(e.path in layout.RESERVED_PACKAGE_FILES for e in pkg.manifest):
        raise UploadRejected("manifest_uses_reserved_name")
    result = verify_tree(pkg.root, pkg.manifest, ignore=set(UNHASHED_PACKAGE_FILES))
    if not result.passed:
        kinds = sorted({k for _, k in result.problems})
        raise UploadRejected("source_integrity_failed", f"{kinds} {result.problems[:5]}")
    files: list[PlannedFile] = []
    for entry in pkg.manifest:
        path = pkg.root.joinpath(*entry.path.split("/"))
        st = path.stat()
        files.append(PlannedFile(entry.path, path, entry.bytes, entry.sha256, st.st_mtime_ns))
    for rel in UNHASHED_PACKAGE_FILES:
        path = pkg.root.joinpath(*rel.split("/"))
        if rel == layout.READY_NAME or not path.is_file():
            continue  # yerel hazır işareti yüklenmez: buluttaki işareti yükleyici üretir
        sha, size = sha256_path(path)
        files.append(PlannedFile(rel, path, size, sha, path.stat().st_mtime_ns))
    manifest_sha, _ = sha256_path(pkg.root / "PACKAGE" / "MANIFEST.json")
    warnings = []
    rs = pkg.release_status
    if not str(rs.get("transfer_authorization") or "").strip():
        warnings.append("transfer_authorization_statement_missing")
    if not any("LICENSE" in e.path.upper() for e in pkg.manifest):
        warnings.append("license_document_not_in_manifest")
    # Manifestte olmayan dosyalar verify_tree'de UNEXPECTED_FILE olarak yukarıda reddedildi (kilit dosyaları hariç).
    assert all(not is_ephemeral(f.path) for f in files)
    return UploadPlan(pkg.root, pkg.package_id, pkg.dataset_id, pkg.version, manifest_sha, access_class_from_release(rs),
                      pkg.quality_status, sorted(files, key=lambda f: f.relpath), warnings)


# --------------------------------------------------------------------------- ilerleme

class ProgressReporter:
    """Aktarılan hacim, hız, kalan bayt ve tahmini süre. Gizli değer yazmaz."""

    def __init__(self, total_bytes: int, total_files: int, out: TextIO | None = None, interval: float = 1.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.total_bytes, self.total_files = total_bytes, total_files
        self.out, self.interval, self.clock = out or sys.stdout, interval, clock
        self.done_bytes = 0
        self.sent_bytes = 0  # bu çalıştırmada gerçekten gönderilen
        self.files_done = 0
        self.started = clock()
        self._last = 0.0

    def add(self, n: int, *, sent: bool = True) -> None:
        self.done_bytes += n
        if sent:
            self.sent_bytes += n
        if self.clock() - self._last >= self.interval:
            self.render()

    def file_done(self) -> None:
        self.files_done += 1

    def snapshot(self) -> dict[str, Any]:
        elapsed = max(self.clock() - self.started, 1e-9)
        speed = self.sent_bytes / elapsed
        remaining = max(self.total_bytes - self.done_bytes, 0)
        return {"files_done": self.files_done, "files_total": self.total_files, "bytes_done": self.done_bytes,
                "bytes_total": self.total_bytes, "bytes_remaining": remaining, "bytes_per_second": speed,
                "eta_seconds": (remaining / speed) if speed > 0 else None}

    def render(self) -> None:
        self._last = self.clock()
        s = self.snapshot()
        eta = "—" if s["eta_seconds"] is None else f"{int(s['eta_seconds'] // 60)}m {int(s['eta_seconds'] % 60)}s"
        self.out.write(f"  [{s['files_done']}/{s['files_total']} dosya] {_human(s['bytes_done'])} / {_human(s['bytes_total'])}"
                       f" · {_human(s['bytes_per_second'])}/s · kalan {_human(s['bytes_remaining'])} · ETA {eta}\n")
        self.out.flush()


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


# --------------------------------------------------------------------------- yükleme

def _read_json(store: ObjectStore, key: str) -> Any:
    with store.open_read(key) as handle:
        return json.loads(handle.read().decode("utf-8"))


def _canonical(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upload_package(store: ObjectStore, root: Path, *, provider: str, bucket: str, authorization_path: Path | None = None,
                   reporter: ProgressReporter | None = None, cancel: CancelToken | None = None,
                   plan: UploadPlan | None = None) -> dict[str, Any]:
    """Paketi yükler. Sonuç: ``{"status": UPLOADED | ALREADY_UPLOADED | ALREADY_PUBLISHED, ...}``.

    Reddedilecek durumlar :class:`UploadRejected`, geçici ağ sorunları :class:`TransientError` yükseltir;
    ikincisinde aynı komut tekrar çalıştırılır ve kaldığı yerden sürer.
    """
    plan = plan or plan_package(root)
    authorization: dict[str, Any] | None = None
    if plan.access_class in auth.NEEDS_AUTHORIZATION:
        if authorization_path is None:
            raise UploadRejected("authorization_required", plan.access_class)
        try:
            authorization = auth.validate(auth.load(authorization_path), dataset_id=plan.dataset_id, version=plan.version,
                                          package_id=plan.package_id, manifest_sha256=plan.manifest_sha256,
                                          access_class=plan.access_class, provider=provider, bucket=bucket)
        except auth.AuthorizationError as exc:
            raise UploadRejected(exc.code, exc.detail) from None

    op = f"upl-{plan.manifest_sha256[:32]}"
    ready = layout.ready_key(plan.package_id)
    existing = store.head(ready)
    if existing is not None:
        marker = _read_json(store, ready)
        if marker.get("manifest_sha256") != plan.manifest_sha256:
            raise UploadRejected("package_id_conflict", "aynı paket kimliğiyle farklı içerik zaten yüklenmiş")
        _announce(store, plan, op)
        return {"status": "ALREADY_UPLOADED", "package_id": plan.package_id, "ready_key": ready}
    try:
        entry_key = layout.catalog_entry_key(plan.dataset_id, plan.version)
        entry = store.head(entry_key)
        if entry is not None:
            published = _read_json(store, entry_key)
            if published.get("manifest_sha256") == plan.manifest_sha256:
                return {"status": "ALREADY_PUBLISHED", "package_id": plan.package_id, "catalog_entry": entry_key}
            raise UploadRejected("version_already_published", f"{plan.dataset_id} {plan.version}")
    except StorageError as exc:
        if exc.code not in ("access_denied", "not_found"):
            raise  # ayrı kabul kovasında katalog okunamaz: bu denetim kabul aşamasında yapılır

    import io

    reporter = reporter or ProgressReporter(plan.total_bytes, len(plan.files), out=io.StringIO())
    prefix = layout.package_prefix(plan.package_id)
    for f in plan.files:
        key = prefix + f.relpath
        st = f.path.stat()
        if (st.st_size, st.st_mtime_ns) != (f.size, f.mtime_ns):
            raise UploadRejected("source_changed_since_verification", f.relpath)
        head = store.head(key)
        if head is not None:
            if head.sha256 == f.sha256 and head.size == f.size:
                reporter.add(f.size, sent=False)  # önceki (kesilmiş) çalıştırmada yüklenmiş
                reporter.file_done()
                continue
            raise UploadRejected("incoming_object_conflict", f.relpath)
        file_op = f"{op}.{hashlib.sha1(f.relpath.encode('utf-8')).hexdigest()[:12]}"
        store.upload_file(key, f.path, expected_sha256=f.sha256, operation_id=file_op, cancel=cancel,
                          progress=reporter.add, source_verified=True)
        reporter.file_done()
    if authorization is not None:
        try:
            store.put_bytes(layout.authorization_key(plan.package_id), _canonical(authorization), create_only=True,
                            operation_id=f"{op}.auth", cancel=cancel)
        except AlreadyExists:
            raise UploadRejected("authorization_conflict") from None
    reporter.render()

    problems = []  # metadata düzeyi: boyut + kayıtlı SHA-256 (tam doğrulama kabul aşamasında yapılır)
    for f in plan.files:
        head = store.head(prefix + f.relpath)
        if head is None:
            problems.append(f"{f.relpath}:MISSING")
        elif head.size != f.size or head.sha256 != f.sha256:
            problems.append(f"{f.relpath}:RECORDED_SHA256_OR_SIZE_MISMATCH")
    if problems:
        raise UploadRejected("uploaded_object_check_failed", "; ".join(problems[:5]))

    marker = {"schema": layout.UPLOAD_SCHEMA, "package_id": plan.package_id, "dataset_id": plan.dataset_id,
              "version": plan.version, "manifest_sha256": plan.manifest_sha256, "access_class": plan.access_class,
              "quality_status_declared": plan.quality_status, "authorization": authorization is not None,
              "file_count": len(plan.files), "total_bytes": plan.total_bytes,
              "files": [{"path": f.relpath, "bytes": f.size, "sha256": f.sha256} for f in plan.files],
              "uploaded_at": _now(), "operation_id": op, "tool": TOOL, "warnings": plan.warnings}
    try:
        store.put_bytes(ready, _canonical(marker), create_only=True, operation_id=f"{op}.ready", cancel=cancel)
    except AlreadyExists:
        current = _read_json(store, ready)
        if current.get("manifest_sha256") != plan.manifest_sha256:
            raise UploadRejected("package_id_conflict") from None
    _announce(store, plan, op)
    return {"status": "UPLOADED", "package_id": plan.package_id, "ready_key": ready, "files": len(plan.files),
            "bytes": plan.total_bytes, "bytes_sent": reporter.sent_bytes}


def _announce(store: ObjectStore, plan: UploadPlan, op: str) -> None:
    body = _canonical({"schema": layout.UPLOAD_SCHEMA, "package_id": plan.package_id, "manifest_sha256": plan.manifest_sha256,
                       "ready_key": layout.ready_key(plan.package_id)})
    try:
        store.put_bytes(layout.announcement_key(plan.package_id), body, create_only=True, operation_id=f"{op}.announce")
    except AlreadyExists:
        pass  # duyuru yalnız bir dizindir; yetkili olan paket önekindeki işarettir


# --------------------------------------------------------------------------- komut satırı

def _store():
    from ..objectstore.factory import cloud_store
    from ..objectstore.prefixed import namespaced
    from ..settings import load_settings

    settings = load_settings()
    intake_bucket = settings.cloud.intake_bucket or settings.cloud.bucket
    store = cloud_store(settings, "uploader", bucket=intake_bucket)
    # Yetki kaydı verinin KALICI olarak saklanacağı kovayı (yayın kovası) adlandırır; kabul kovası geçicidir.
    return namespaced(store, settings.cloud.namespace), settings.cloud.provider, settings.cloud.bucket or intake_bucket


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="thermalis-upload", description="THERMALIS paket yükleyici (R2)")
    sub = parser.add_subparsers(dest="command", required=True)
    up = sub.add_parser("upload", help="Hazır paketi R2'ye yükle")
    up.add_argument("package", help="Paket kök klasörü (içinde PACKAGE/ bulunan)")
    up.add_argument("--authorization", default=None, help="RESTRICTED/UNKNOWN paket için THERMALIS bulut yetki kaydı (JSON)")
    up.add_argument("--dry-run", action="store_true", help="Yalnız doğrula; ağ kullanma, hiçbir şey yükleme")
    tpl = sub.add_parser("authorization-template", help="Paket için doldurulacak yetki kaydı şablonunu yazdır")
    tpl.add_argument("package")
    cfg = sub.add_parser("configure", help="R2 uç noktası ve kova (gizli değer değil)")
    cfg.add_argument("--endpoint", required=True)
    cfg.add_argument("--bucket", required=True)
    cfg.add_argument("--intake-bucket", default=None)
    sub.add_parser("credentials", help="uploader kimlik bilgisini Windows Credential Manager'a yaz (yankısız)")
    sub.add_parser("status", help="Yapılandırma ve kimlik bilgisi durumu (değer gösterilmez)")
    args = parser.parse_args(argv)
    # pixi görevi src/ içinde çalışır: göreli yollar komutun çağrıldığı klasöre göre çözülür.
    caller = Path(os.environ.get("THERMALIS_CALLER_CWD") or os.environ.get("INIT_CWD") or os.getcwd())
    for name in ("package", "authorization"):
        value = getattr(args, name, None)
        if value and not Path(value).is_absolute():
            setattr(args, name, str(caller / value))

    from ..cli import main as cli_main

    if args.command == "configure":
        extra = ["--intake-bucket", args.intake_bucket] if args.intake_bucket else []
        return cli_main(["storage", "configure-cloud", "--endpoint", args.endpoint, "--bucket", args.bucket, *extra])
    if args.command == "credentials":
        return cli_main(["storage", "credentials", "set", "--role", "uploader"])
    if args.command == "status":
        return cli_main(["storage", "status"])

    try:
        plan = plan_package(Path(args.package))
    except UploadRejected as exc:
        print(f"REDDEDİLDİ / REJECTED: {exc.code} {exc.detail}", file=sys.stderr)
        return 2
    print(f"Paket / package: {plan.package_id}  {plan.dataset_id} {plan.version}")
    print(f"Erişim / access: {plan.access_class} · kalite / quality: {plan.quality_status} · "
          f"{len(plan.files)} dosya · {_human(plan.total_bytes)} · MANIFEST SHA-256 {plan.manifest_sha256}")
    for w in plan.warnings:
        print(f"  UYARI / WARNING: {w}")
    if args.command == "authorization-template":
        from ..settings import load_settings

        s = load_settings()
        print(json.dumps(auth.template(dataset_id=plan.dataset_id, version=plan.version, package_id=plan.package_id,
                                       manifest_sha256=plan.manifest_sha256, access_class=plan.access_class,
                                       provider=s.cloud.provider, bucket=s.cloud.bucket or ""),
                         ensure_ascii=False, indent=2))
        return 0
    if args.dry_run:
        print("Kuru çalıştırma: SHA-256 ve yapı doğrulandı; hiçbir şey yüklenmedi / dry run: nothing uploaded")
        return 0
    try:
        store, provider, bucket = _store()
        reporter = ProgressReporter(plan.total_bytes, len(plan.files))
        result = upload_package(store, plan.root, provider=provider, bucket=bucket,
                                authorization_path=Path(args.authorization) if args.authorization else None,
                                reporter=reporter, plan=plan)
    except UploadRejected as exc:
        print(f"REDDEDİLDİ / REJECTED: {exc.code} {exc.detail}", file=sys.stderr)
        return 2
    except (TransientError, CredentialsMissing, StorageError) as exc:
        print(f"BAŞARISIZ / FAILED: {exc.code} {exc.detail} — aynı komutla yeniden deneyin (kaldığı yerden sürer)", file=sys.stderr)
        return 3
    print(f"SONUÇ / RESULT: {result['status']}  {result.get('ready_key', result.get('catalog_entry', ''))}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
