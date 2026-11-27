"""SHA-256 adresli, sınırlı, atılabilir yerel önbellek.

    <önbellek>/blobs/<ab>/<sha256>          doğrulanmış içerik (salt okunur)
    <önbellek>/tmp/<sha256>.<rastgele>.part  süren indirmeler (tamamlanınca atomik olarak blobs/ altına)
    <önbellek>/leases/<sha256>.<rastgele>    kullanımdaki içerik (tahliye edilmez)
    <önbellek>/materialized/<ad>-<özet>/     çok dosyalı veri setinin klasör yapısı (blob'lara sabit bağlantı)

Kurallar:
- İçerik yalnız tam SHA-256 doğrulamasından sonra önbelleğe girer.
- Önbellek kalıcı veri kökünün ve gelen paket klasörünün DIŞINDA olmalıdır; tahliye yalnız
  ``blobs/`` ve ``materialized/`` altında çalışır. Kaynak veri ve yerel sürümler asla silinmez.
- Kapasite aşılırsa en uzun süredir kullanılmayan (LRU) ve kiralanmamış içerik çıkarılır.
- Metadata denetimi (varlık + boyut) ile tam kriptografik doğrulama (SHA-256) ayrı işlemlerdir.
- Önbellek silinse bile depodan yeniden kurulur.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .base import CHUNK, CancelToken, ObjectStore, is_sha256_hex, sha256_path
from .errors import ChecksumMismatch, InvalidKey, StorageError
from .keys import validate_key

DEFAULT_MAX_BYTES = 20 * 1024 ** 3
STALE_TMP_SECONDS = 3600
STALE_LEASE_SECONDS = 24 * 3600
WINDOWS_MAX_PATH = 259
MANIFEST_NAME = ".thermalis-materialized.json"
RANGE_CHUNK = 8 * 1024 * 1024


class CacheFull(StorageError):
    code = "cache_full"


@dataclass(frozen=True)
class CacheItem:
    key: str          # depo anahtarı
    relpath: str      # malzeme klasöründeki göreli yol ("/" ayırıcılı)
    sha256: str
    size: int
    offset: int | None = None  # verilirse nesnenin [offset, offset+size) aralığı (ör. TAR üyesi); yalnız o aralık indirilir
    codec: str | None = None   # "deflate": aralık ZIP üyesinin sıkıştırılmış verisidir; yerelde açılır, açılmış içerik doğrulanır
    stored_size: int | None = None  # sıkıştırılmış aralık uzunluğu (codec verildiğinde)


def default_cache_dir() -> Path:
    from platformdirs import user_cache_dir

    return Path(user_cache_dir("THERMALIS", appauthor=False)) / "objects"


def _inside(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _writable_unlink(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    except OSError:
        pass
    path.unlink(missing_ok=True)


class ObjectCache:
    def __init__(self, root: Path, max_bytes: int = DEFAULT_MAX_BYTES, *, protected_roots: tuple[Path, ...] | list[Path] = ()) -> None:
        self.root = Path(root)
        if max_bytes < 1024 * 1024:
            raise ValueError("max_bytes must be >= 1 MiB")
        self.max_bytes = max_bytes
        for protected in protected_roots:
            protected = Path(protected)
            if _inside(self.root, protected) or _inside(protected, self.root):
                raise InvalidKey("cache_dir_overlaps_protected_data", key=str(protected.name))
        self._lock = threading.RLock()
        self._fetch_locks: dict[str, threading.Lock] = {}
        for sub in ("blobs", "tmp", "leases", "materialized"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.cleanup_incomplete()

    # -- yollar ---------------------------------------------------------------------------------
    def blob_path(self, sha256: str) -> Path:
        if not is_sha256_hex(sha256):
            raise InvalidKey("invalid_sha256", key=str(sha256)[:16])
        return self.root / "blobs" / sha256[:2] / sha256

    # -- bakım ----------------------------------------------------------------------------------
    def cleanup_incomplete(self, *, stale_seconds: float = STALE_TMP_SECONDS) -> int:
        """Yarım indirmeleri ve süresi geçmiş kiraları temizler (çökme sonrası kurtarma)."""
        removed = 0
        now = time.time()
        for path in (self.root / "tmp").iterdir():
            try:
                if now - path.stat().st_mtime >= stale_seconds:
                    if path.is_dir():
                        import shutil

                        shutil.rmtree(path, ignore_errors=True)
                    else:
                        _writable_unlink(path)
                    removed += 1
            except OSError:
                continue
        for path in (self.root / "leases").iterdir():
            try:
                if now - path.stat().st_mtime >= STALE_LEASE_SECONDS:
                    path.unlink(missing_ok=True)
            except OSError:
                continue
        return removed

    def usage(self) -> int:
        total = 0
        for path in (self.root / "blobs").rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        return total

    def _leased(self) -> set[str]:
        return {p.name.split(".", 1)[0] for p in (self.root / "leases").iterdir()}

    @contextmanager
    def lease(self, sha256: str) -> Iterator[None]:
        self.blob_path(sha256)
        path = self.root / "leases" / f"{sha256}.{os.getpid()}-{uuid.uuid4().hex[:12]}"
        path.write_bytes(b"")
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    def _evict(self, needed: int) -> None:
        with self._lock:
            if needed > self.max_bytes:
                raise CacheFull(f"object_larger_than_cache:{needed}")
            blobs = [p for p in (self.root / "blobs").rglob("*") if p.is_file()]
            used = sum(p.stat().st_size for p in blobs)
            if used + needed <= self.max_bytes:
                return
            leased = self._leased()
            for path in sorted(blobs, key=lambda p: p.stat().st_mtime):  # LRU: erişimde zaman damgası güncellenir
                if used + needed <= self.max_bytes:
                    break
                if path.name in leased:
                    continue
                size = path.stat().st_size
                try:
                    _writable_unlink(path)
                except OSError:  # Windows: başka bir süreç dosyayı açık tutuyor → kullanımda say
                    continue
                self._drop_materialized_using(path.name)
                used -= size
            if used + needed > self.max_bytes:
                raise CacheFull("all_remaining_entries_are_in_use")

    def _drop_materialized_using(self, sha256: str) -> None:
        import shutil

        for folder in (self.root / "materialized").iterdir():
            manifest = folder / MANIFEST_NAME
            try:
                shas = {i["sha256"] for i in json.loads(manifest.read_text(encoding="utf-8"))["items"]}
            except (OSError, ValueError, KeyError):
                continue
            if sha256 in shas and not (shas & self._leased()):
                for p in folder.rglob("*"):
                    if p.is_file():
                        try:
                            os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
                        except OSError:
                            pass
                shutil.rmtree(folder, ignore_errors=True)

    # -- doğrulama -------------------------------------------------------------------------------
    def contains(self, sha256: str, size: int | None = None) -> bool:
        """Yalnız metadata denetimi: içerik var mı, boyutu doğru mu (baytlar okunmaz)."""
        path = self.blob_path(sha256)
        try:
            st = path.stat()
        except FileNotFoundError:
            return False
        return size is None or st.st_size == size

    def verify(self, sha256: str) -> bool:
        """Tam kriptografik doğrulama. Bozuk içerik önbellekten çıkarılır (sonraki erişimde yeniden indirilir)."""
        path = self.blob_path(sha256)
        if not path.is_file():
            return False
        actual, _ = sha256_path(path)
        if actual != sha256:
            _writable_unlink(path)
            self._drop_materialized_using(sha256)
            return False
        return True

    # -- getirme ---------------------------------------------------------------------------------
    def fetch(self, store: ObjectStore, key: str, sha256: str, size: int, *, full_verify: bool = False,
              cancel: CancelToken | None = None, offset: int | None = None, codec: str | None = None,
              stored_size: int | None = None) -> Path:
        """İçeriği önbelleğe getirir ve yolunu döndürür.

        Önbellekte varsa varsayılan denetim metadata düzeyindedir (boyut); ``full_verify=True`` ise
        SHA-256 yeniden hesaplanır ve bozuksa içerik yeniden indirilir.

        ``offset`` verilirse yalnız ``[offset, offset+size)`` bayt aralığı parça parça (``Range`` istekleriyle)
        indirilir ve aralığın SHA-256'sı beklenen özetle karşılaştırılır (ör. arşiv içindeki tek bir COG).
        """
        validate_key(key)
        path = self.blob_path(sha256)
        with self._lock:
            fetch_lock = self._fetch_locks.setdefault(sha256, threading.Lock())
        with fetch_lock:
            if self.contains(sha256, size) and (not full_verify or self.verify(sha256)):
                os.utime(path)  # LRU
                return path
            if path.exists():  # boyutu yanlış: bozuk
                _writable_unlink(path)
            self._evict(size)
            tmp = self.root / "tmp" / f"{sha256}.{uuid.uuid4().hex[:12]}.part"
            import hashlib

            digest = hashlib.sha256()
            written = 0
            try:
                with tmp.open("wb") as dst:
                    if offset is None:
                        with store.open_read(key) as src:
                            for block in iter(lambda: src.read(CHUNK), b""):
                                if cancel is not None:
                                    cancel.raise_if_cancelled(key)
                                digest.update(block)
                                written += len(block)
                                dst.write(block)
                    else:
                        import zlib

                        if codec not in (None, "stored", "deflate"):
                            raise ChecksumMismatch(f"unsupported_codec:{codec}", key=key)
                        inflater = zlib.decompressobj(-15) if codec == "deflate" else None
                        pos = offset
                        end = offset + (stored_size if codec else size)
                        while pos < end:
                            if cancel is not None:
                                cancel.raise_if_cancelled(key)
                            block = store.read_range(key, pos, min(pos + RANGE_CHUNK, end) - 1)
                            if not block:
                                raise ChecksumMismatch("range_read_returned_no_data", key=key)
                            pos += len(block)
                            piece = block if inflater is None else inflater.decompress(block, RANGE_CHUNK)
                            while True:  # sınırlı bellek: her adımda en çok RANGE_CHUNK açılmış bayt
                                digest.update(piece)
                                written += len(piece)
                                dst.write(piece)
                                if inflater is None or not inflater.unconsumed_tail:
                                    break
                                piece = inflater.decompress(inflater.unconsumed_tail, RANGE_CHUNK)
                        if inflater is not None:
                            tail = inflater.flush()
                            digest.update(tail)
                            written += len(tail)
                            dst.write(tail)
                    dst.flush()
                    os.fsync(dst.fileno())
                if written != size or digest.hexdigest() != sha256:
                    raise ChecksumMismatch("downloaded_content_does_not_match", key=key)
                os.chmod(tmp, stat.S_IREAD)
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(tmp, path)
                except PermissionError:
                    if not self.contains(sha256, size):  # başka okuyucu aynı içeriği koydu ve açık tutuyor
                        raise
            finally:
                if tmp.exists():
                    _writable_unlink(tmp)
            return path

    @contextmanager
    def materialize(self, store: ObjectStore, name: str, items: list[CacheItem], *, full_verify: bool = False,
                    cancel: CancelToken | None = None) -> Iterator[Path]:
        """Çok dosyalı veri setini (ör. File Geodatabase klasörü) klasör yapısıyla hazırlar.

        Blok süresince tüm içerik kiralanır (tahliye edilmez). Dosyalar blob'lara sabit bağlantıdır
        (bağlantı yoksa kopyalanır) ve salt okunurdur.
        """
        import hashlib

        if not name or any(c in name for c in '/\\:*?"<>|') or name.startswith("."):
            raise InvalidKey("invalid_materialization_name", key=name)
        for item in items:
            validate_key(item.relpath)
        manifest = json.dumps({"items": [i.__dict__ for i in sorted(items, key=lambda i: i.relpath)]}, sort_keys=True)
        folder = self.root / "materialized" / f"{name}-{hashlib.sha256(manifest.encode('utf-8')).hexdigest()[:16]}"
        leases = []
        try:
            for item in items:
                cm = self.lease(item.sha256)
                cm.__enter__()
                leases.append(cm)
            for item in items:
                self.fetch(store, item.key, item.sha256, item.size, full_verify=full_verify, cancel=cancel, offset=item.offset,
                           codec=item.codec, stored_size=item.stored_size)
            with self._lock:
                if not self._materialized_ok(folder, items):
                    self._build(folder, items, manifest)
            yield folder
        finally:
            for cm in reversed(leases):
                cm.__exit__(None, None, None)

    def _materialized_ok(self, folder: Path, items: list[CacheItem]) -> bool:
        if not (folder / MANIFEST_NAME).is_file():
            return False
        for item in items:
            path = folder.joinpath(*item.relpath.split("/"))
            try:
                if path.stat().st_size != item.size:
                    return False
            except OSError:
                return False
        return True

    def _build(self, folder: Path, items: list[CacheItem], manifest: str) -> None:
        import shutil

        staging = self.root / "tmp" / f"mat-{uuid.uuid4().hex[:12]}"
        try:
            for item in items:
                dest = staging.joinpath(*item.relpath.split("/"))
                final = folder.joinpath(*item.relpath.split("/"))
                if os.name == "nt" and len(str(final)) > WINDOWS_MAX_PATH:
                    raise StorageError("windows_path_too_long_choose_shorter_cache_dir", key=item.relpath)
                dest.parent.mkdir(parents=True, exist_ok=True)
                blob = self.blob_path(item.sha256)
                try:
                    os.link(blob, dest)
                except OSError:
                    shutil.copyfile(blob, dest)
                    os.chmod(dest, stat.S_IREAD)
            (staging / MANIFEST_NAME).write_text(manifest, encoding="utf-8")
            if folder.exists():
                self._drop_dir(folder)
            os.replace(staging, folder)
        finally:
            if staging.exists():
                self._drop_dir(staging)

    @staticmethod
    def _drop_dir(folder: Path) -> None:
        import shutil

        for p in folder.rglob("*"):
            if p.is_file():
                try:
                    os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
                except OSError:
                    pass
        shutil.rmtree(folder, ignore_errors=True)
