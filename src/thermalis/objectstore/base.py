"""Sağlayıcıdan bağımsız nesne deposu arayüzü.

Bu arayüz bir dosya sistemi taklidi **değildir**: klasör, yeniden adlandırma, sona ekleme ve
silme yoktur. Nesneler anahtar → bayt dizisidir. Desteklenen işlemler:

- ``head`` / ``exists``           metadata ve varlık (yalnız metadata; kriptografik doğrulama değil)
- ``list``                        önek bazlı, sayfalı, anahtar sırasına göre listeleme
- ``open_read`` / ``read_range``  akış hâlinde okuma ve bayt aralığı okuma
- ``put_bytes(..., create_only)`` atomik yalnız-oluştur yazım (S3: ``If-None-Match: *``)
- ``put_bytes(..., if_match=)``   koşullu güncelleme (S3: ``If-Match: <etag>``)
- ``upload_file``                 büyük dosya; çok parçalı, sürdürülebilir, yalnız-oluştur tamamlama
- ``verify_object``               nesnenin TAMAMINI okuyup SHA-256 karşılaştırma (tam doğrulama)
- ``gdal_path``                   isteğe bağlı GDAL erişim yolu (yerel yol veya ``/vsis3/``)

ETag yalnız koşullu güncellemelerde **sürüm doğrulayıcı** olarak kullanılır; içerik özeti değildir.
İçerik bütünlüğü her zaman SHA-256 ile (yazarken hesaplanan ve nesne metadata'sında saklanan)
gösterilir ve tam doğrulama için nesne baştan sona okunur.
"""

from __future__ import annotations

import hashlib
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator

from .errors import Cancelled, ChecksumMismatch, NotFound

META_SHA256 = "thermalis-sha256"
META_OPERATION = "thermalis-operation-id"
CHUNK = 4 * 1024 * 1024
_HEX64 = frozenset("0123456789abcdef")


def is_sha256_hex(value: str | None) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= _HEX64


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    size: int
    etag: str  # sürüm doğrulayıcı (opaque). İçerik özeti DEĞİLDİR.
    last_modified: str | None = None
    sha256: str | None = None  # yazarken hesaplanıp metadata'da saklanan SHA-256 (yoksa None: bilinmiyor)
    operation_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Capabilities:
    backend: str
    create_only_put: bool
    conditional_update: bool
    range_read: bool
    multipart_upload: bool
    resumable_multipart: bool
    server_side_metadata: bool
    local_path: bool
    gdal_access: str | None  # "local" | "vsis3" | None
    atomic_rename: bool = False  # yalnız yerel; S3 tarafında yeniden adlandırma yoktur
    delete: bool = False  # uygulama arayüzünde silme bilinçli olarak yok

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class VerificationResult:
    key: str
    level: str  # "metadata" (boyut + kayıtlı özet) | "full" (tüm bayt okunup SHA-256)
    ok: bool
    expected_sha256: str | None
    actual_sha256: str | None
    expected_size: int | None
    actual_size: int | None
    problem: str | None = None


class CancelToken:
    """İş birlikçi iptal. Uzun işlemler parça aralarında ve yeniden deneme beklemelerinde denetler."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self, key: str | None = None) -> None:
        if self._event.is_set():
            raise Cancelled("operation_cancelled", key=key)

    def wait(self, seconds: float) -> bool:
        """``seconds`` kadar bekler; iptal edilirse erken döner (True)."""
        return self._event.wait(seconds)


NEVER_CANCELLED = CancelToken()


def sha256_stream(stream: BinaryIO, cancel: CancelToken | None = None, key: str | None = None) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    for block in iter(lambda: stream.read(CHUNK), b""):
        if cancel is not None:
            cancel.raise_if_cancelled(key)
        digest.update(block)
        total += len(block)
    return digest.hexdigest(), total


def sha256_path(path: Path, cancel: CancelToken | None = None) -> tuple[str, int]:
    with Path(path).open("rb") as handle:
        return sha256_stream(handle, cancel)


class ObjectStore(ABC):
    """Tüm arka uçların uyması gereken sözleşme (bkz. ``tests/test_objectstore_contract.py``)."""

    @property
    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def head(self, key: str) -> ObjectInfo | None: ...

    def exists(self, key: str) -> bool:
        return self.head(key) is not None

    @abstractmethod
    def list(self, prefix: str = "", *, page_size: int = 1000) -> Iterator[ObjectInfo]:
        """Anahtar sırasıyla (UTF-8 bayt sırası) tüm nesneler; sağlayıcı sayfalarını kendisi izler."""

    @abstractmethod
    def open_read(self, key: str) -> BinaryIO:
        """Akış hâlinde okuma. Çağıran kapatır (``with`` ile)."""

    @abstractmethod
    def read_range(self, key: str, start: int, end: int) -> bytes:
        """``[start, end]`` (iki uç dahil, HTTP Range gibi) baytları."""

    @abstractmethod
    def put_bytes(self, key: str, data: bytes, *, create_only: bool = False, if_match: str | None = None,
                  operation_id: str | None = None, metadata: dict[str, str] | None = None,
                  cancel: CancelToken | None = None) -> ObjectInfo:
        """Küçük nesne yazımı. ``create_only`` ve ``if_match`` birlikte verilemez.

        ``create_only``: nesne varsa :class:`AlreadyExists`; ancak var olan nesne **aynı işlem
        kimliği ve aynı SHA-256** ile yazılmışsa (belirsiz yazımın tekrarı) başarı sayılır.
        ``if_match``: nesnenin ETag'i eşleşmezse :class:`PreconditionFailed`.
        Hiçbiri verilmezse koşulsuz yazım yapılmaz: arka uç ``unconditional_writes`` desteklemiyorsa reddeder.
        """

    @abstractmethod
    def upload_file(self, key: str, path: Path, *, expected_sha256: str, operation_id: str,
                    metadata: dict[str, str] | None = None, cancel: CancelToken | None = None) -> ObjectInfo:
        """Büyük dosyayı yalnız-oluştur olarak yükler; kesilirse aynı ``operation_id`` ile sürdürülür."""

    def gdal_path(self, key: str) -> str | None:
        return None

    def local_path(self, key: str) -> Path | None:
        return None

    # -- doğrulama (arka uçtan bağımsız) ----------------------------------------------------
    def check_metadata(self, key: str, *, expected_size: int | None = None,
                       expected_sha256: str | None = None) -> VerificationResult:
        """Yalnız metadata denetimi: varlık, boyut ve **kayıtlı** SHA-256. Baytlar okunmaz."""
        info = self.head(key)
        if info is None:
            return VerificationResult(key, "metadata", False, expected_sha256, None, expected_size, None, "MISSING")
        problem = None
        if expected_size is not None and info.size != expected_size:
            problem = "SIZE_MISMATCH"
        elif expected_sha256 and info.sha256 and info.sha256 != expected_sha256:
            problem = "RECORDED_SHA256_MISMATCH"
        return VerificationResult(key, "metadata", problem is None, expected_sha256, info.sha256, expected_size, info.size, problem)

    def verify_object(self, key: str, expected_sha256: str, *, expected_size: int | None = None,
                      cancel: CancelToken | None = None) -> VerificationResult:
        """Tam kriptografik doğrulama: nesne baştan sona okunur ve SHA-256 hesaplanır."""
        try:
            with self.open_read(key) as stream:
                actual, size = sha256_stream(stream, cancel, key)
        except NotFound:
            return VerificationResult(key, "full", False, expected_sha256, None, expected_size, None, "MISSING")
        problem = None
        if expected_size is not None and size != expected_size:
            problem = "SIZE_MISMATCH"
        elif actual != expected_sha256:
            problem = "SHA256_MISMATCH"
        return VerificationResult(key, "full", problem is None, expected_sha256, actual, expected_size, size, problem)

    def require_verified(self, key: str, expected_sha256: str, *, expected_size: int | None = None,
                         cancel: CancelToken | None = None) -> VerificationResult:
        result = self.verify_object(key, expected_sha256, expected_size=expected_size, cancel=cancel)
        if not result.ok:
            raise ChecksumMismatch(result.problem or "", key=key)
        return result
