"""Depolama hataları: sağlayıcıdan bağımsız, yapılandırılmış ve gizli bilgi içermeyen.

Her hata dilden bağımsız bir ``code`` taşır (arayüz ve günlükler bu koda göre çevirir).
Mesajlarda kimlik bilgisi, imzalı URL veya istek başlığı bulunmaz; ``detail`` yalnız anahtar
(key), işlem kimliği ve durum kodu gibi gizli olmayan bilgileri içerir.
"""

from __future__ import annotations


class StorageError(Exception):
    code = "storage_error"
    retryable = False

    def __init__(self, detail: str = "", *, key: str | None = None, status: int | None = None) -> None:
        self.detail = detail
        self.key = key
        self.status = status
        super().__init__(self.__str__())

    def __str__(self) -> str:
        parts = [self.code]
        if self.key:
            parts.append(f"key={self.key}")
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.detail:
            parts.append(self.detail)
        return " ".join(parts)

    def as_dict(self) -> dict:
        return {"code": self.code, "key": self.key, "status": self.status, "detail": self.detail}


class InvalidKey(StorageError):
    code = "invalid_key"


class NotFound(StorageError):
    code = "not_found"


class AlreadyExists(StorageError):
    """Yalnız-oluştur (create-only) yazımı: hedef anahtar başka bir içerikle zaten var (412)."""

    code = "already_exists"


class PreconditionFailed(StorageError):
    """Koşullu güncelleme: nesne beklenen sürümde değil (If-Match → 412)."""

    code = "precondition_failed"


class ObjectLocked(StorageError):
    """Kova kilidi (bucket lock) politikası işlemi engelledi (R2: 409 ObjectLockedByBucketPolicy)."""

    code = "object_locked"


class ChecksumMismatch(StorageError):
    code = "checksum_mismatch"


class RangeNotSatisfiable(StorageError):
    code = "range_not_satisfiable"


class AccessDenied(StorageError):
    code = "access_denied"


class CredentialsMissing(StorageError):
    code = "credentials_missing"


class CapabilityNotSupported(StorageError):
    code = "capability_not_supported"


class Cancelled(StorageError):
    code = "cancelled"


class TransientError(StorageError):
    """Ağ hatası, zaman aşımı, 5xx veya 429. Sınırlı sayıda yeniden denenir; sonra bu hata yükselir."""

    code = "transient_error"
    retryable = True


class AmbiguousWrite(StorageError):
    """Yazımın sonucu belirsiz kaldı ve uzlaştırma (reconciliation) sonucu da belirlenemedi."""

    code = "ambiguous_write"


class UploadStateInvalid(StorageError):
    code = "upload_state_invalid"


class PublicationError(StorageError):
    code = "publication_error"


class PublicationNotAuthorized(StorageError):
    code = "publication_not_authorized"
