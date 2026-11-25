"""S3 uyumlu arka uç (Cloudflare R2 için yapılandırılmış).

Koşullu yazımlar (R2 belgesi: https://developers.cloudflare.com/r2/api/s3/conditional-operations/):
- yalnız-oluştur:      ``PutObject`` + ``If-None-Match: *``
- koşullu güncelleme:  ``PutObject`` + ``If-Match: <etag>``
- çok parçalı:         ``CompleteMultipartUpload`` + ``If-None-Match: *`` (başlatmada değil, tamamlamada)
SDK bu parametreleri sunmazsa arka uç başlatılmaz (:func:`require_conditional_support`); koşulsuz yazıma
sessizce düşülmez. "Var mı bak, sonra yaz" hiçbir yerde koşullu işlemin yerine kullanılmaz.

Belirsiz yazımlar (ağ kesildi, sonuç bilinmiyor) işlem kimliğiyle uzlaştırılır: her yazım nesne
metadata'sına ``thermalis-operation-id`` ve ``thermalis-sha256`` yazar. Tekrar denemede 412 alınırsa
nesnenin metadata'sı okunur; işlem kimliği ve SHA-256 bizimkiyle aynıysa yazım bizimdir (başarı),
değilse gerçek bir çakışmadır (:class:`AlreadyExists` / :class:`PreconditionFailed`).

ETag yalnız ``If-Match`` doğrulayıcısıdır; içerik özeti olarak KULLANILMAZ (çok parçalı nesnelerde zaten değildir).
"""

from __future__ import annotations

import io
import random
import re
from contextlib import contextmanager
from datetime import timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

from .base import (CHUNK, META_OPERATION, META_SHA256, NEVER_CANCELLED, Capabilities, CancelToken, ObjectInfo, ObjectStore,
                   is_sha256_hex, sha256_path)
from .errors import (AccessDenied, AlreadyExists, AmbiguousWrite, CapabilityNotSupported, ChecksumMismatch,
                     CredentialsMissing, InvalidKey, NotFound, ObjectLocked, PreconditionFailed, RangeNotSatisfiable,
                     StorageError, TransientError, UploadStateInvalid)
from .keys import validate_key, validate_prefix
from .multipart import UploadState, UploadStateStore, validate_operation_id
from .retry import RetryPolicy, call_with_retry

MIB = 1024 * 1024
MIN_PART_SIZE = 5 * MIB
MAX_PARTS = 10_000
_META_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_TRANSIENT_CODES = {"SlowDown", "InternalError", "ServiceUnavailable", "RequestTimeout", "RequestTimeTooSkewed",
                    "ConditionalRequestConflict", "TooManyRequests", "OperationAborted"}


def require_conditional_support(client: Any) -> None:
    """Seçilen SDK'nın gereken koşullu parametreleri sunduğunu doğrular; yoksa arka uç kullanılmaz."""
    model = getattr(getattr(client, "meta", None), "service_model", None)
    if model is None:  # test istemcisi: sözleşmeyi kendisi uygular
        return
    for op, names in (("PutObject", ("IfNoneMatch", "IfMatch")), ("CompleteMultipartUpload", ("IfNoneMatch",))):
        members = model.operation_model(op).input_shape.members
        missing = [n for n in names if n not in members]
        if missing:
            raise CapabilityNotSupported(f"sdk_missing_conditional_parameters:{op}:{','.join(missing)}")


def map_error(exc: BaseException, key: str | None = None) -> StorageError:
    """botocore istisnasını yapılandırılmış ve GİZLİ BİLGİ İÇERMEYEN depolama hatasına çevirir.

    Sağlayıcının hata mesajı kopyalanmaz (imzalı istek ayrıntıları içerebilir); yalnız hata kodu ve HTTP durumu.
    """
    if isinstance(exc, StorageError):
        return exc
    try:
        from botocore import exceptions as bex
    except ImportError:  # pragma: no cover
        bex = None
    if bex is not None:
        if isinstance(exc, bex.ClientError):
            err = exc.response.get("Error", {}) or {}
            code = str(err.get("Code") or "")
            status = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
            if status == 412 or code == "PreconditionFailed":
                return PreconditionFailed(code or "PreconditionFailed", key=key, status=412)
            if code == "ObjectLockedByBucketPolicy" or (status == 409 and "Lock" in code):
                return ObjectLocked(code, key=key, status=status)
            if code in ("NoSuchKey", "NotFound", "404") or (status == 404 and code != "NoSuchUpload" and code != "NoSuchBucket"):
                return NotFound(code or "NotFound", key=key, status=404)
            if code == "NoSuchUpload":
                return NotFound("NoSuchUpload", key=key, status=status)
            if status == 416 or code == "InvalidRange":
                return RangeNotSatisfiable(code or "InvalidRange", key=key, status=416)
            if status in (401, 403) or code in ("AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch"):
                return AccessDenied(code or "AccessDenied", key=key, status=status)
            if status == 429 or (status is not None and status >= 500) or code in _TRANSIENT_CODES:
                return TransientError(code or "server_error", key=key, status=status)
            return StorageError(code or "client_error", key=key, status=status)
        if isinstance(exc, (bex.NoCredentialsError, bex.PartialCredentialsError)):
            return CredentialsMissing("no_credentials", key=key)
        transient = tuple(getattr(bex, n) for n in ("EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError",
                                                    "ConnectionClosedError", "ResponseStreamingError", "ProxyConnectionError",
                                                    "IncompleteReadError") if hasattr(bex, n))
        if isinstance(exc, transient):
            return TransientError(type(exc).__name__, key=key)
        if isinstance(exc, bex.BotoCoreError):
            return StorageError(type(exc).__name__, key=key)
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return TransientError(type(exc).__name__, key=key)
    return StorageError(f"unexpected:{type(exc).__name__}", key=key)


def _clean_etag(value: Any) -> str:
    return str(value or "")


def _info_from_head(key: str, resp: dict) -> ObjectInfo:
    meta = {str(k).lower(): str(v) for k, v in (resp.get("Metadata") or {}).items()}
    lm = resp.get("LastModified")
    sha = meta.get(META_SHA256)
    return ObjectInfo(key=key, size=int(resp.get("ContentLength", 0)), etag=_clean_etag(resp.get("ETag")),
                      last_modified=lm.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(lm, "astimezone") else None,
                      sha256=sha if is_sha256_hex(sha) else None, operation_id=meta.get(META_OPERATION),
                      metadata={k: v for k, v in meta.items() if k not in (META_SHA256, META_OPERATION)})


def _validate_metadata(metadata: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in (metadata or {}).items():
        if not _META_KEY_RE.match(k) or k in (META_SHA256, META_OPERATION) or not isinstance(v, str) or not v.isascii() or len(v) > 512:
            raise InvalidKey("invalid_metadata", key=k)
        out[k] = v
    return out


class _ResumingReader(io.RawIOBase):
    """``GetObject`` gövdesi; akış ortasında geçici hata olursa ``Range`` + ``If-Match`` ile kaldığı yerden sürer.

    ``If-Match`` aynı nesne sürümünü garanti eder: nesne bu arada değiştiyse okuma 412 ile durur (karışık içerik yok).
    """

    def __init__(self, backend: "S3Backend", key: str, cancel: CancelToken) -> None:
        super().__init__()
        self._b, self._key, self._cancel = backend, key, cancel
        self._pos = 0
        self._etag: str | None = None
        self._size: int | None = None
        self._body = None
        self._open()

    def _open(self) -> None:
        def attempt(_: int):
            params: dict[str, Any] = {"Bucket": self._b.bucket, "Key": self._key}
            if self._etag is not None:
                params["IfMatch"] = self._etag
                params["Range"] = f"bytes={self._pos}-"
            return self._b._raw("get_object", self._key, **params)

        resp = call_with_retry(attempt, policy=self._b.retry, cancel=self._cancel, key=self._key, rng=self._b.rng)
        if self._etag is None:
            self._etag = _clean_etag(resp.get("ETag"))
            self._size = int(resp.get("ContentLength", 0))
        self._body = resp["Body"]

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if self._size is not None and self._pos >= self._size:
            return 0
        for attempt in range(self._b.retry.max_attempts):
            self._cancel.raise_if_cancelled(self._key)
            try:
                chunk = self._body.read(len(buffer))
                break
            except Exception as exc:  # noqa: BLE001 - eşlenip sınıflandırılır
                mapped = map_error(exc, self._key)
                if not mapped.retryable or attempt == self._b.retry.max_attempts - 1:
                    raise mapped from None
                self._cancel.wait(self._b.retry.delay(attempt, self._b.rng))
                self._open()
        n = len(chunk)
        buffer[:n] = chunk
        self._pos += n
        if n == 0 and self._size is not None and self._pos < self._size:
            raise TransientError("truncated_body", key=self._key)
        return n

    def close(self) -> None:
        try:
            if self._body is not None:
                self._body.close()
        finally:
            super().close()


class S3Backend(ObjectStore):
    def __init__(self, client: Any, bucket: str, *, retry: RetryPolicy | None = None,
                 state_store: UploadStateStore | None = None, part_size: int = 64 * MIB,
                 multipart_threshold: int = 64 * MIB, rng: random.Random | None = None, gdal_prefix: str = "/vsis3/") -> None:
        if not re.match(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$", bucket or ""):
            raise InvalidKey("invalid_bucket_name", key=bucket)
        if part_size < MIN_PART_SIZE:
            raise ValueError("part_size must be >= 5 MiB")
        require_conditional_support(client)
        self.client = client
        self.bucket = bucket
        self.retry = retry or RetryPolicy()
        self.state_store = state_store
        self.part_size = part_size
        self.multipart_threshold = max(multipart_threshold, MIN_PART_SIZE)
        self.rng = rng or random.Random()
        self.gdal_prefix = gdal_prefix

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(backend="s3", create_only_put=True, conditional_update=True, range_read=True,
                            multipart_upload=True, resumable_multipart=self.state_store is not None,
                            server_side_metadata=True, local_path=False, gdal_access="vsis3", atomic_rename=False, delete=False)

    # -- düşük seviye -----------------------------------------------------------------------------
    def _raw(self, op: str, key: str | None, **params: Any) -> Any:
        try:
            return getattr(self.client, op)(**params)
        except Exception as exc:  # noqa: BLE001 - tümü yapılandırılmış hataya çevrilir
            raise map_error(exc, key) from None

    def _call(self, op: str, key: str | None, cancel: CancelToken | None = None, **params: Any) -> Any:
        return call_with_retry(lambda _: self._raw(op, key, **params), policy=self.retry, cancel=cancel, key=key, rng=self.rng)

    # -- okuma -------------------------------------------------------------------------------------
    def head(self, key: str) -> ObjectInfo | None:
        validate_key(key)
        try:
            resp = self._call("head_object", key, Bucket=self.bucket, Key=key)
        except NotFound:
            return None
        return _info_from_head(key, resp)

    def list(self, prefix: str = "", *, page_size: int = 1000) -> Iterator[ObjectInfo]:
        validate_prefix(prefix)
        if not 1 <= page_size <= 1000:
            raise ValueError("page_size must be within 1..1000")
        token: str | None = None
        while True:
            params: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": page_size}
            if token:
                params["ContinuationToken"] = token
            resp = self._call("list_objects_v2", prefix or None, **params)
            for item in resp.get("Contents", []) or []:
                lm = item.get("LastModified")
                yield ObjectInfo(key=item["Key"], size=int(item.get("Size", 0)), etag=_clean_etag(item.get("ETag")),
                                 last_modified=lm.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if hasattr(lm, "astimezone") else None)
            if not resp.get("IsTruncated"):
                return
            token = resp.get("NextContinuationToken")
            if not token:
                raise StorageError("pagination_token_missing", key=prefix)

    def open_read(self, key: str, *, cancel: CancelToken | None = None) -> BinaryIO:
        validate_key(key)
        return io.BufferedReader(_ResumingReader(self, key, cancel or NEVER_CANCELLED), buffer_size=CHUNK)

    def read_range(self, key: str, start: int, end: int) -> bytes:
        validate_key(key)
        if start < 0 or end < start:
            raise RangeNotSatisfiable("invalid_range", key=key)
        resp = self._call("get_object", key, Bucket=self.bucket, Key=key, Range=f"bytes={start}-{end}")
        try:
            return resp["Body"].read()
        except Exception as exc:  # noqa: BLE001
            raise map_error(exc, key) from None

    def gdal_path(self, key: str) -> str | None:
        return f"{self.gdal_prefix}{self.bucket}/{validate_key(key)}"

    # -- yazım: uzlaştırma ---------------------------------------------------------------------------
    def _reconcile(self, key: str, sha256: str, operation_id: str | None, conflict: type[StorageError]) -> ObjectInfo:
        """412 sonrası: var olan nesne aynı işlemin aynı içeriği mi?"""
        info = self.head(key)
        if info is not None and operation_id and info.operation_id == operation_id and info.sha256 == sha256:
            return info
        raise conflict("different_operation_or_content", key=key, status=412)

    def _conditional_put(self, key: str, body_factory: Callable[[], Any], size: int, sha256: str, *, create_only: bool,
                         if_match: str | None, operation_id: str | None, metadata: dict[str, str],
                         cancel: CancelToken | None) -> ObjectInfo:
        meta = {**metadata, META_SHA256: sha256}
        if operation_id:
            meta[META_OPERATION] = validate_operation_id(operation_id)
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": key, "Metadata": meta, "ContentLength": size}
        if create_only:
            params["IfNoneMatch"] = "*"
        else:
            params["IfMatch"] = if_match
        conflict = AlreadyExists if create_only else PreconditionFailed
        attempts = {"n": 0}

        def attempt(_: int):
            attempts["n"] += 1
            body = body_factory()
            try:
                return self._raw("put_object", key, Body=body, **params)
            finally:
                if hasattr(body, "close"):
                    body.close()

        try:
            resp = call_with_retry(attempt, policy=self.retry, cancel=cancel, key=key, rng=self.rng)
        except PreconditionFailed:
            # İlk denemede 412: gerçek çakışma olabilir ya da daha önceki (belirsiz) bir denememizin sonucu.
            return self._reconcile(key, sha256, operation_id, conflict)
        except TransientError as exc:
            # Denemeler tükendi; yazım uygulanmış olabilir. Son bir kez uzlaştır.
            try:
                info = self.head(key)
            except StorageError:
                raise AmbiguousWrite("write_outcome_unknown", key=key) from exc
            if info is not None and operation_id and info.operation_id == operation_id and info.sha256 == sha256:
                return info
            raise
        return ObjectInfo(key=key, size=size, etag=_clean_etag(resp.get("ETag")), sha256=sha256,
                          operation_id=operation_id, metadata=dict(metadata))

    def put_bytes(self, key: str, data: bytes, *, create_only: bool = False, if_match: str | None = None,
                  operation_id: str | None = None, metadata: dict[str, str] | None = None,
                  cancel: CancelToken | None = None) -> ObjectInfo:
        import hashlib

        validate_key(key)
        if create_only == (if_match is not None):
            raise CapabilityNotSupported("exactly_one_of_create_only_or_if_match_required", key=key)
        return self._conditional_put(key, lambda: io.BytesIO(data), len(data), hashlib.sha256(data).hexdigest(),
                                     create_only=create_only, if_match=if_match, operation_id=operation_id,
                                     metadata=_validate_metadata(metadata), cancel=cancel)

    # -- yazım: dosya / çok parçalı ------------------------------------------------------------------
    def _part_size_for(self, size: int) -> int:
        needed = -(-size // MAX_PARTS)
        part = max(self.part_size, needed)
        return -(-part // MIB) * MIB

    def upload_file(self, key: str, path: Path, *, expected_sha256: str, operation_id: str,
                    metadata: dict[str, str] | None = None, cancel: CancelToken | None = None) -> ObjectInfo:
        validate_key(key)
        validate_operation_id(operation_id)
        cancel = cancel or NEVER_CANCELLED
        meta = _validate_metadata(metadata)
        path = Path(path)
        if not is_sha256_hex(expected_sha256):
            raise ChecksumMismatch("expected_sha256_invalid", key=key)
        st = path.stat()
        actual, size = sha256_path(path, cancel)  # yüklemeden ÖNCE tam dosya SHA-256
        if actual != expected_sha256 or size != st.st_size:
            raise ChecksumMismatch("source_sha256_mismatch", key=key)
        if size <= self.multipart_threshold:
            return self._conditional_put(key, lambda: path.open("rb"), size, expected_sha256, create_only=True, if_match=None,
                                         operation_id=operation_id, metadata=meta, cancel=cancel)
        if self.state_store is None:
            raise CapabilityNotSupported("multipart_requires_state_store", key=key)
        return self._multipart(key, path, st, expected_sha256, operation_id, meta, cancel)

    def _multipart(self, key: str, path: Path, st, sha256: str, operation_id: str, metadata: dict[str, str],
                   cancel: CancelToken) -> ObjectInfo:
        assert self.state_store is not None
        state = self.state_store.load(operation_id)
        if state is None:
            state = UploadState(operation_id=operation_id, bucket=self.bucket, key=key, source_path=str(path),
                                source_size=st.st_size, source_mtime_ns=st.st_mtime_ns, sha256=sha256,
                                part_size=self._part_size_for(st.st_size))
        elif (state.bucket, state.key, state.sha256, state.source_size) != (self.bucket, key, sha256, st.st_size):
            raise UploadStateInvalid("state_does_not_match_request", key=key)
        if state.source_mtime_ns != st.st_mtime_ns:
            raise UploadStateInvalid("source_changed_since_upload_started", key=key)

        if state.upload_id is None:
            resp = self._call("create_multipart_upload", key, cancel, Bucket=self.bucket, Key=key,
                              Metadata={**metadata, META_SHA256: sha256, META_OPERATION: operation_id})
            state.upload_id = resp["UploadId"]
            state.parts = {}
            self.state_store.save(state)
        else:
            server = self._server_parts(state, cancel)
            if server is None:
                # Yükleme sunucuda yok: ya tamamlandı (belirsiz tamamlama) ya da süresi doldu/iptal edildi.
                done = self.head(key)
                if done is not None and done.operation_id == operation_id and done.sha256 == sha256:
                    self.state_store.finish(operation_id)
                    return done
                state.upload_id, state.parts = None, {}
                self.state_store.save(state)
                return self._multipart(key, path, st, sha256, operation_id, metadata, cancel)
            # Sunucunun bildiği parçalar esas alınır; yerel kayıtta olup sunucuda olmayan parça yeniden yüklenir.
            state.parts = {n: p for n, p in server.items() if p["size"] == state.part_bounds(int(n))[1]}
            self.state_store.save(state)

        with path.open("rb") as handle:
            for number in range(1, state.part_count + 1):
                if str(number) in state.parts:
                    continue
                cancel.raise_if_cancelled(key)
                start, length = state.part_bounds(number)
                handle.seek(start)
                data = handle.read(length)
                if len(data) != length:
                    raise UploadStateInvalid("source_shorter_than_recorded", key=key)
                resp = self._call("upload_part", key, cancel, Bucket=self.bucket, Key=key, UploadId=state.upload_id,
                                  PartNumber=number, Body=data, ContentLength=length)
                state.parts[str(number)] = {"etag": _clean_etag(resp.get("ETag")), "size": length}
                self.state_store.save(state)
        if path.stat().st_mtime_ns != state.source_mtime_ns or path.stat().st_size != state.source_size:
            raise UploadStateInvalid("source_changed_during_upload", key=key)
        info = self._complete(key, state, cancel)
        self.state_store.finish(operation_id)
        return info

    def _server_parts(self, state: UploadState, cancel: CancelToken) -> dict[str, dict] | None:
        parts: dict[str, dict] = {}
        marker = 0
        while True:
            try:
                resp = self._call("list_parts", state.key, cancel, Bucket=self.bucket, Key=state.key,
                                  UploadId=state.upload_id, PartNumberMarker=marker, MaxParts=1000)
            except NotFound:
                return None
            for p in resp.get("Parts", []) or []:
                parts[str(p["PartNumber"])] = {"etag": _clean_etag(p.get("ETag")), "size": int(p.get("Size", 0))}
            if not resp.get("IsTruncated"):
                return parts
            marker = int(resp.get("NextPartNumberMarker") or 0)

    def _complete(self, key: str, state: UploadState, cancel: CancelToken) -> ObjectInfo:
        parts = [{"PartNumber": int(n), "ETag": p["etag"]} for n, p in sorted(state.parts.items(), key=lambda kv: int(kv[0]))]
        if len(parts) != state.part_count:
            raise UploadStateInvalid("parts_missing_before_completion", key=key)
        try:
            resp = self._call("complete_multipart_upload", key, cancel, Bucket=self.bucket, Key=key, UploadId=state.upload_id,
                              MultipartUpload={"Parts": parts}, IfNoneMatch="*")
        except (PreconditionFailed, NotFound) as exc:
            # 412: anahtar zaten var. NotFound(NoSuchUpload): önceki (belirsiz) tamamlama başarılı olmuş olabilir.
            info = self.head(key)
            if info is not None and info.operation_id == state.operation_id and info.sha256 == state.sha256:
                return info
            if isinstance(exc, PreconditionFailed):
                self._abort_own_upload(state)
                self.state_store.finish(state.operation_id) if self.state_store else None
                raise AlreadyExists("different_operation_or_content", key=key, status=412) from None
            raise
        except TransientError as exc:
            info = self.head(key)
            if info is not None and info.operation_id == state.operation_id and info.sha256 == state.sha256:
                return info
            raise AmbiguousWrite("complete_outcome_unknown_retry_with_same_operation_id", key=key) from exc
        head = self.head(key)
        return head or ObjectInfo(key=key, size=state.source_size, etag=_clean_etag(resp.get("ETag")), sha256=state.sha256,
                                  operation_id=state.operation_id)

    def _abort_own_upload(self, state: UploadState) -> None:
        """Yalnız bu işlemin tamamlanmamış çok parçalı yüklemesini bırakır (yayımlanmış nesneye dokunmaz)."""
        try:
            self._raw("abort_multipart_upload", state.key, Bucket=self.bucket, Key=state.key, UploadId=state.upload_id)
        except StorageError:
            pass


# --------------------------------------------------------------------------- istemci ve GDAL

def validate_endpoint(endpoint: str) -> str:
    """Yalnız ``https://<host>`` biçimi (yol, sorgu, kullanıcı bilgisi yok). Hesap kimliği kodda sabit değildir."""
    from urllib.parse import urlsplit

    parts = urlsplit(endpoint or "")
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.path not in ("", "/") \
            or parts.query or parts.fragment:
        raise InvalidKey("invalid_endpoint", key=None)
    return f"https://{parts.netloc}"


def make_client(endpoint: str, credential, *, region: str = "auto", connect_timeout: float = 10.0,
                read_timeout: float = 60.0) -> Any:
    """R2/S3 istemcisi. Kimlik bilgisi yalnız bellekte tutulur; ortam değişkenine/dosyaya yazılmaz.

    - ``region='auto'`` (R2), path-style adresleme, SigV4.
    - Zaman aşımları açık; botocore'un kendi yeniden denemesi kapalı (tek katman: :mod:`.retry`).
    - Varsayılan esnek bütünlük (CRC) başlıkları yalnız gerektiğinde: bütünlük SHA-256 ile ayrıca doğrulanır.
    """
    import boto3
    from botocore.config import Config

    session = boto3.session.Session(aws_access_key_id=credential.access_key_id,
                                    aws_secret_access_key=credential.secret_access_key.reveal())
    config = Config(signature_version="s3v4", region_name=region, s3={"addressing_style": "path"},
                    connect_timeout=connect_timeout, read_timeout=read_timeout,
                    retries={"total_max_attempts": 1, "mode": "standard"},
                    request_checksum_calculation="when_required", response_checksum_validation="when_required")
    client = session.client("s3", endpoint_url=validate_endpoint(endpoint), config=config)
    require_conditional_support(client)
    return client


@contextmanager
def gdal_vsis3_session(bucket: str, endpoint: str, credential, *, region: str = "auto"):
    """GDAL ``/vsis3/`` erişimi için yalnız bu kova yoluna özel, geçici yapılandırma.

    Kimlik bilgisi ortam değişkenine yazılmaz; ``SetPathSpecificOption`` ile verilip blok sonunda silinir.
    Büyük bulut-yerel formatlar (COG/GeoParquet/Zarr) için hazırlık; File Geodatabase önbellekten yerel okunur.
    """
    from osgeo import gdal

    prefix = f"/vsis3/{bucket}"
    host = validate_endpoint(endpoint)[len("https://"):]
    options = {"AWS_S3_ENDPOINT": host, "AWS_HTTPS": "YES", "AWS_VIRTUAL_HOSTING": "FALSE", "AWS_REGION": region,
               "AWS_ACCESS_KEY_ID": credential.access_key_id, "AWS_SECRET_ACCESS_KEY": credential.secret_access_key.reveal()}
    try:
        for name, value in options.items():
            gdal.SetPathSpecificOption(prefix, name, value)
        yield prefix
    finally:
        gdal.ClearPathSpecificOptions(prefix)
