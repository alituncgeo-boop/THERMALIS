"""Testler için bellek içi S3 sunucusu + boto3 istemci taklidi.

R2'nin belgelenmiş davranışını izler: koşullu PutObject (If-None-Match: * / If-Match), koşullu
CompleteMultipartUpload, sayfalı ListObjectsV2, Range okuma (416), çok parçalı yüklemede son parça
hariç eşit parça boyutu. İşlemler tek kilit altında atomiktir (sunucu tarafı koşul denetimi gibi).
Hata enjeksiyonu: bağlantı hatası (uygulanmadan) ve "uygulandıktan sonra zaman aşımı" (belirsiz yazım).

Bu bir taklittir; gerçek R2 uyumluluğunu kanıtlamaz.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
from botocore.response import StreamingBody

_MIN_PART = 5 * 1024 * 1024


def _err(code: str, status: int, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} (fake)"}, "ResponseMetadata": {"HTTPStatusCode": status}}, op)


class _FlakyBody:
    """Belirli bayttan sonra bir kez zaman aşımı veren gövde (akış ortası kesinti)."""

    def __init__(self, data: bytes, fail_after: int) -> None:
        self._raw = io.BytesIO(data)
        self._fail_after = fail_after

    def read(self, amt=None):
        if self._raw.tell() >= self._fail_after:
            raise ReadTimeoutError(endpoint_url="https://fake")
        limit = self._fail_after - self._raw.tell()
        return self._raw.read(min(amt or limit, limit))

    def close(self):
        pass


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}
        self.uploads: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.calls: dict[str, int] = defaultdict(int)
        self.faults: list[tuple[str, str]] = []  # (op, "before" | "after")
        self.flaky_reads: list[int] = []  # sıradaki get_object gövdesi bu bayttan sonra kopar
        self.corrupt_on_write: set[str] = set()  # bu son eklerle biten anahtarlar sessizce bozulur (depolama hatası taklidi)
        self._etags = itertools.count(1)
        self.meta = None  # gerçek istemci değil: koşul desteği sözleşme gereği burada uygulanır

    # -- hata enjeksiyonu --------------------------------------------------------------------------
    def fail_next(self, op: str, when: str = "before", times: int = 1) -> None:
        self.faults.extend([(op, when)] * times)

    def _fault(self, op: str, when: str) -> bool:
        if (op, when) in self.faults:
            self.faults.remove((op, when))
            return True
        return False

    def _run(self, op: str, fn):
        self.calls[op] += 1
        if self._fault(op, "before"):
            raise EndpointConnectionError(endpoint_url="https://fake")
        with self.lock:
            result = fn()
        if self._fault(op, "after"):
            raise ReadTimeoutError(endpoint_url="https://fake")  # sunucu uyguladı, istemci yanıtı alamadı
        return result

    def _etag(self) -> str:
        return f'"etag-{next(self._etags)}"'

    @staticmethod
    def _body(body) -> bytes:
        if isinstance(body, (bytes, bytearray)):
            return bytes(body)
        return body.read()

    # -- S3 işlemleri --------------------------------------------------------------------------------
    def head_object(self, Bucket, Key, **kw):
        def go():
            obj = self.objects.get(Key)
            if obj is None:
                raise _err("404", 404, "HeadObject")
            return {"ContentLength": len(obj["body"]), "ETag": obj["etag"], "Metadata": dict(obj["meta"]),
                    "LastModified": obj["modified"]}
        return self._run("head_object", go)

    def get_object(self, Bucket, Key, Range=None, IfMatch=None, **kw):
        def go():
            obj = self.objects.get(Key)
            if obj is None:
                raise _err("NoSuchKey", 404, "GetObject")
            if IfMatch is not None and IfMatch != obj["etag"]:
                raise _err("PreconditionFailed", 412, "GetObject")
            data = obj["body"]
            if Range:
                start, end = Range[len("bytes="):].split("-")
                start = int(start)
                end = int(end) if end else len(data) - 1
                if start >= len(data):
                    raise _err("InvalidRange", 416, "GetObject")
                data = data[start:min(end, len(data) - 1) + 1]
            if self.flaky_reads:
                body = _FlakyBody(data, self.flaky_reads.pop(0))
            else:
                body = StreamingBody(io.BytesIO(data), len(data))
            return {"Body": body, "ContentLength": len(data), "ETag": obj["etag"], "Metadata": dict(obj["meta"])}
        return self._run("get_object", go)

    def put_object(self, Bucket, Key, Body, Metadata=None, IfNoneMatch=None, IfMatch=None, ContentLength=None, **kw):
        data = self._body(Body)

        def go():
            current = self.objects.get(Key)
            if IfNoneMatch == "*" and current is not None:
                raise _err("PreconditionFailed", 412, "PutObject")
            if IfMatch is not None and (current is None or current["etag"] != IfMatch):
                raise _err("PreconditionFailed", 412, "PutObject")
            if IfNoneMatch is None and IfMatch is None:
                self.calls["unconditional_put"] += 1
            if current is not None and current.get("locked"):
                raise _err("ObjectLockedByBucketPolicy", 409, "PutObject")
            etag = self._etag()
            stored = data[:-1] + bytes([data[-1] ^ 1]) if data and any(Key.endswith(s) for s in self.corrupt_on_write) else data
            self.objects[Key] = {"body": stored, "etag": etag, "meta": {k.lower(): v for k, v in (Metadata or {}).items()},
                                 "modified": datetime.now(timezone.utc)}
            return {"ETag": etag}
        return self._run("put_object", go)

    def list_objects_v2(self, Bucket, Prefix="", MaxKeys=1000, ContinuationToken=None, **kw):
        def go():
            keys = sorted((k for k in self.objects if k.startswith(Prefix)), key=lambda k: k.encode("utf-8"))
            start = int(ContinuationToken) if ContinuationToken else 0
            page = keys[start:start + MaxKeys]
            truncated = start + MaxKeys < len(keys)
            out = {"Contents": [{"Key": k, "Size": len(self.objects[k]["body"]), "ETag": self.objects[k]["etag"],
                                 "LastModified": self.objects[k]["modified"]} for k in page],
                   "IsTruncated": truncated, "KeyCount": len(page)}
            if truncated:
                out["NextContinuationToken"] = str(start + MaxKeys)
            return out
        return self._run("list_objects_v2", go)

    def create_multipart_upload(self, Bucket, Key, Metadata=None, **kw):
        def go():
            upload_id = f"upl-{len(self.uploads) + 1}"
            self.uploads[upload_id] = {"key": Key, "parts": {}, "meta": {k.lower(): v for k, v in (Metadata or {}).items()}}
            return {"UploadId": upload_id}
        return self._run("create_multipart_upload", go)

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body, ContentLength=None, **kw):
        data = self._body(Body)

        def go():
            up = self.uploads.get(UploadId)
            if up is None or up["key"] != Key:
                raise _err("NoSuchUpload", 404, "UploadPart")
            etag = f'"part-{hashlib.md5(data).hexdigest()}"'
            up["parts"][PartNumber] = {"etag": etag, "body": data}
            return {"ETag": etag}
        return self._run("upload_part", go)

    def list_parts(self, Bucket, Key, UploadId, PartNumberMarker=0, MaxParts=1000, **kw):
        def go():
            up = self.uploads.get(UploadId)
            if up is None:
                raise _err("NoSuchUpload", 404, "ListParts")
            numbers = sorted(n for n in up["parts"] if n > PartNumberMarker)
            page = numbers[:MaxParts]
            out = {"Parts": [{"PartNumber": n, "ETag": up["parts"][n]["etag"], "Size": len(up["parts"][n]["body"])} for n in page],
                   "IsTruncated": len(numbers) > MaxParts}
            if out["IsTruncated"]:
                out["NextPartNumberMarker"] = page[-1]
            return out
        return self._run("list_parts", go)

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload, IfNoneMatch=None, IfMatch=None, **kw):
        def go():
            up = self.uploads.get(UploadId)
            if up is None:
                raise _err("NoSuchUpload", 404, "CompleteMultipartUpload")
            if IfNoneMatch == "*" and Key in self.objects:
                raise _err("PreconditionFailed", 412, "CompleteMultipartUpload")
            if IfNoneMatch is None and IfMatch is None:
                self.calls["unconditional_complete"] += 1
            parts = MultipartUpload["Parts"]
            bodies = []
            for i, p in enumerate(parts):
                stored = up["parts"].get(p["PartNumber"])
                if stored is None or stored["etag"] != p["ETag"]:
                    raise _err("InvalidPart", 400, "CompleteMultipartUpload")
                bodies.append(stored["body"])
            sizes = [len(b) for b in bodies]
            if any(s != sizes[0] for s in sizes[:-1]) or any(s < _MIN_PART for s in sizes[:-1]) or sizes[-1] > sizes[0]:
                raise _err("InvalidPart", 400, "CompleteMultipartUpload")  # R2: son parça hariç eşit boyut
            etag = f'"mp-{len(parts)}-{next(self._etags)}"'
            self.objects[Key] = {"body": b"".join(bodies), "etag": etag, "meta": up["meta"], "modified": datetime.now(timezone.utc)}
            del self.uploads[UploadId]
            return {"ETag": etag}
        return self._run("complete_multipart_upload", go)

    def abort_multipart_upload(self, Bucket, Key, UploadId, **kw):
        def go():
            self.uploads.pop(UploadId, None)
            return {}
        return self._run("abort_multipart_upload", go)


def make_s3_backend(tmp_path: Path, **kwargs):
    from thermalis.objectstore.multipart import UploadStateStore
    from thermalis.objectstore.retry import RetryPolicy
    from thermalis.objectstore.s3 import S3Backend

    server = FakeS3()
    options = {"retry": RetryPolicy(max_attempts=4, base_delay=0.0, max_delay=0.0),
               "state_store": UploadStateStore(tmp_path / "upload-state"),
               "part_size": _MIN_PART, "multipart_threshold": _MIN_PART}
    options.update(kwargs)
    return S3Backend(server, "thermalis-test-bucket", **options), server
