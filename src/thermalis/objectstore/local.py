"""Yerel dosya sistemi arka ucu (varsayılan).

Mevcut veri kökünü olduğu gibi okur: ``LocalBackend(<veri kökü>/data)`` ile ``datasets/...``
anahtarları bugünkü dosyalara karşılık gelir. Okuma işlemleri hiçbir şey yazmaz.

Yazım semantiği (yalnız yeni nesneler için; mevcut kayıt akışı Faz 02'de değişmedi):
- yalnız-oluştur: geçici dosya → ``fsync`` → ``os.link`` (hedef varsa atomik olarak başarısız olur;
  bağlantı desteklenmezse Windows'ta ``os.rename``, o da var olan hedefe yazmaz). Var olan hedefin
  üzerine yazılması mümkün değildir.
- koşullu güncelleme: anahtar başına süreçler arası kilit altında ETag karşılaştırması + ``os.replace``.
- metadata (SHA-256, işlem kimliği, ETag) ``.thermalis-store/meta/`` altında yan dosyada tutulur.
  Yan dosyası olmayan (ör. mevcut, bu arayüzden önce yazılmış) dosyalarda SHA-256 *bilinmiyor* kalır
  ve ETag dosya boyutu + değişiklik zamanından türetilir (yalnız doğrulayıcı).
"""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

from ..storage import write_json_atomic
from .base import (CHUNK, META_OPERATION, META_SHA256, Capabilities, CancelToken, ObjectInfo, ObjectStore,
                   is_sha256_hex, sha256_path)
from .errors import (AlreadyExists, CapabilityNotSupported, ChecksumMismatch, InvalidKey, NotFound,
                     PreconditionFailed, RangeNotSatisfiable, StorageError, TransientError)
from .keys import INTERNAL_PREFIX, validate_key, validate_prefix

STORE_DIR = ".thermalis-store"
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
LOCK_TIMEOUT_SECONDS = 10.0


def _is_link(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _KeyLock:
    """Anahtar başına süreçler arası kilit (O_EXCL). Yalnız kısa koşullu güncellemeler için."""

    def __init__(self, path: Path, key: str) -> None:
        self.path = path
        self.key = key

    def __enter__(self) -> "_KeyLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii"))
                os.close(fd)
                return self
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise TransientError("local_lock_busy", key=self.key)
                time.sleep(0.02)

    def __exit__(self, *exc) -> None:
        self.path.unlink(missing_ok=True)


class LocalBackend(ObjectStore):
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(backend="local", create_only_put=True, conditional_update=True, range_read=True,
                            multipart_upload=True, resumable_multipart=False, server_side_metadata=True,
                            local_path=True, gdal_access="local", atomic_rename=True, delete=False)

    # -- yol güvenliği ------------------------------------------------------------------------
    def _path(self, key: str) -> Path:
        validate_key(key)
        path = self.root.joinpath(*key.split("/"))
        current = self.root
        for part in key.split("/"):
            current = current / part
            if _is_link(current):
                raise InvalidKey("symlink_or_junction_in_path", key=key)
        return path

    def _internal(self, *parts: str) -> Path:
        return self.root.joinpath(STORE_DIR, *parts)

    def _meta_path(self, key: str) -> Path:
        return self._internal("meta", *key.split("/")[:-1], key.split("/")[-1] + ".json")

    def _read_meta(self, key: str, st: os.stat_result) -> dict | None:
        path = self._meta_path(key)
        try:
            with path.open("r", encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            return None
        # Yan dosya ancak dosyanın kendisiyle tutarlıysa geçerlidir (dışarıdan değiştirilmiş dosya → bilinmiyor).
        if meta.get("size") != st.st_size or meta.get("mtime_ns") != st.st_mtime_ns:
            return None
        return meta

    def _info(self, key: str, path: Path) -> ObjectInfo | None:
        try:
            st = path.stat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        meta = self._read_meta(key, st)
        if meta:
            return ObjectInfo(key, st.st_size, meta["etag"], _iso(st.st_mtime), meta.get("sha256"), meta.get("operation_id"),
                              dict(meta.get("metadata") or {}))
        return ObjectInfo(key, st.st_size, f'"st-{st.st_size}-{st.st_mtime_ns}"', _iso(st.st_mtime))

    # -- okuma ---------------------------------------------------------------------------------
    def head(self, key: str) -> ObjectInfo | None:
        return self._info(key, self._path(key))

    def list(self, prefix: str = "", *, page_size: int = 1000) -> Iterator[ObjectInfo]:
        validate_prefix(prefix)
        base = self.root.joinpath(*prefix.rstrip("/").split("/")) if prefix else self.root
        if not base.is_dir() or (prefix and _is_link(base)):
            return
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            current = Path(dirpath)
            # Bağlantı/junction izlenmez; arka ucun iç klasörü listelenmez.
            dirnames[:] = [d for d in dirnames if not d.startswith(INTERNAL_PREFIX) and not _is_link(current / d)]
            for name in filenames:
                if name.startswith(INTERNAL_PREFIX) or _is_link(current / name):
                    continue
                found.append((current / name).relative_to(self.root).as_posix())
        for key in sorted(found, key=lambda k: k.encode("utf-8")):
            info = self._info(key, self.root.joinpath(*key.split("/")))
            if info is not None:
                yield info

    def open_read(self, key: str) -> BinaryIO:
        path = self._path(key)
        try:
            return path.open("rb")
        except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
            if path.is_file():
                raise StorageError("local_read_failed", key=key) from exc
            raise NotFound(key=key) from exc

    def read_range(self, key: str, start: int, end: int) -> bytes:
        if start < 0 or end < start:
            raise RangeNotSatisfiable("invalid_range", key=key)
        info = self.head(key)
        if info is None:
            raise NotFound(key=key)
        if start >= info.size:
            raise RangeNotSatisfiable(f"start={start} size={info.size}", key=key)
        with self.open_read(key) as handle:
            handle.seek(start)
            return handle.read(min(end, info.size - 1) - start + 1)

    def local_path(self, key: str) -> Path | None:
        path = self._path(key)
        return path if path.is_file() else None

    def gdal_path(self, key: str) -> str | None:
        return str(self._path(key))

    # -- yazım ---------------------------------------------------------------------------------
    def _tmp(self) -> Path:
        folder = self._internal("tmp")
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{INTERNAL_PREFIX}{uuid.uuid4().hex}"

    def _write_meta(self, key: str, path: Path, sha256: str, operation_id: str | None, metadata: dict[str, str]) -> ObjectInfo:
        st = path.stat()
        etag = f'"{uuid.uuid4().hex}"'
        write_json_atomic(self._meta_path(key), {"sha256": sha256, "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                                                 "etag": etag, "operation_id": operation_id, "metadata": metadata})
        return ObjectInfo(key, st.st_size, etag, _iso(st.st_mtime), sha256, operation_id, dict(metadata))

    def _publish_create_only(self, key: str, tmp: Path, sha256: str, operation_id: str | None,
                             metadata: dict[str, str]) -> ObjectInfo:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._path(key)  # klasörler oluşturulduktan sonra bağlantı denetimi tekrar
        try:
            try:
                os.link(tmp, target)
            except FileExistsError:
                raise
            except (OSError, NotImplementedError, AttributeError):
                if os.name != "nt":
                    raise CapabilityNotSupported("hardlink_unavailable_for_create_only", key=key)
                os.rename(tmp, target)  # Windows: hedef varsa FileExistsError (üzerine yazmaz)
        except FileExistsError:
            tmp.unlink(missing_ok=True)
            return self._reconcile_existing(key, sha256, operation_id)
        tmp.unlink(missing_ok=True)
        return self._write_meta(key, target, sha256, operation_id, metadata)

    def _reconcile_existing(self, key: str, sha256: str, operation_id: str | None) -> ObjectInfo:
        """Var olan hedef: aynı işlemin aynı içerikli tekrarı mı (başarı), yoksa çakışma mı."""
        info = self.head(key)
        if info is None:
            raise AlreadyExists("target_appeared_and_vanished", key=key)
        if info.sha256 is not None:
            if info.sha256 == sha256 and info.operation_id == operation_id and operation_id is not None:
                return info
            raise AlreadyExists("different_operation_or_content", key=key)
        # Yan dosya yok (bağlantı sonrası, metadata yazımından önce kesilmiş olabilir): içerik karşılaştırılır.
        actual, _ = sha256_path(self._path(key))
        if actual == sha256 and operation_id is not None:
            return self._write_meta(key, self._path(key), sha256, operation_id, {})
        raise AlreadyExists("different_content", key=key)

    def put_bytes(self, key: str, data: bytes, *, create_only: bool = False, if_match: str | None = None,
                  operation_id: str | None = None, metadata: dict[str, str] | None = None,
                  cancel: CancelToken | None = None) -> ObjectInfo:
        import hashlib

        validate_key(key)
        if create_only == (if_match is not None):
            raise CapabilityNotSupported("exactly_one_of_create_only_or_if_match_required", key=key)
        if cancel is not None:
            cancel.raise_if_cancelled(key)
        sha256 = hashlib.sha256(data).hexdigest()
        meta = dict(metadata or {})
        tmp = self._tmp()
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if create_only:
            return self._publish_create_only(key, tmp, sha256, operation_id, meta)
        target = self._path(key)
        try:
            with _KeyLock(self._internal("locks", f"{uuid.uuid5(uuid.NAMESPACE_URL, key).hex}.lock"), key):
                current = self.head(key)
                if current is None or current.etag != if_match:
                    raise PreconditionFailed("etag_mismatch" if current else "object_missing", key=key, status=412)
                for attempt in range(5):
                    try:
                        os.replace(tmp, target)
                        break
                    except PermissionError:  # Windows: okuyucu dosyayı açık tutuyor
                        if attempt == 4:
                            raise TransientError("local_replace_blocked", key=key)
                        time.sleep(0.05 * (attempt + 1))
                return self._write_meta(key, target, sha256, operation_id, meta)
        finally:
            tmp.unlink(missing_ok=True)

    def upload_file(self, key: str, path: Path, *, expected_sha256: str, operation_id: str,
                    metadata: dict[str, str] | None = None, cancel: CancelToken | None = None,
                    progress=None, source_verified: bool = False) -> ObjectInfo:
        """Yerel "çok parçalı" yükleme: parça parça kopya + kopya sırasında SHA-256 + yalnız-oluştur yayın."""
        import hashlib

        validate_key(key)
        if not is_sha256_hex(expected_sha256):
            raise ChecksumMismatch("expected_sha256_invalid", key=key)
        tmp = self._tmp()
        digest = hashlib.sha256()
        try:
            with Path(path).open("rb") as src, tmp.open("wb") as dst:
                for block in iter(lambda: src.read(CHUNK), b""):
                    if cancel is not None:
                        cancel.raise_if_cancelled(key)
                    digest.update(block)
                    dst.write(block)
                    if progress is not None:
                        progress(len(block))
                dst.flush()
                os.fsync(dst.fileno())
            if digest.hexdigest() != expected_sha256:
                raise ChecksumMismatch("source_sha256_mismatch", key=key)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return self._publish_create_only(key, tmp, expected_sha256, operation_id, dict(metadata or {}))


__all__ = ["LocalBackend", "META_SHA256", "META_OPERATION"]
