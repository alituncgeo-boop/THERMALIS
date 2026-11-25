"""Canlı R2 duman testi (YALNIZ ELLE, açık onayla çalıştırılır; otomatik testlerde ve CI'da çalışmaz).

Tüm nesneler benzersiz ``smoke-tests/<çalıştırma>/`` öneki altında, rastgele üretilmiş SENTETİK
baytlardır. Gerçek veri, HD01, kayıtlı katalog içeriği veya hassas metadata asla kullanılmaz.
Uygulama silme işlemi sunmadığı için test nesneleri yerinde kalır; temizlik için
``smoke-tests/`` önekine kısa ömürlü bir R2 yaşam döngüsü (lifecycle) kuralı önerilir (bkz. docs).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

from .base import ObjectInfo, ObjectStore
from .errors import AlreadyExists, PreconditionFailed, StorageError
from .publication import COMMIT_NAME, PublicationPolicy, ReleaseFile, committed_releases, inspect_release, publish_release

LIVE_ENV = "THERMALIS_R2_LIVE_TEST"


class PrefixedStore(ObjectStore):
    """Tüm anahtarları sabit bir önek altına alan sarmalayıcı (duman testinin yalıtımı için)."""

    def __init__(self, inner: ObjectStore, prefix: str) -> None:
        self.inner, self.prefix = inner, prefix

    @property
    def capabilities(self):
        return self.inner.capabilities

    def _k(self, key: str) -> str:
        return self.prefix + key

    def _strip(self, info: ObjectInfo | None) -> ObjectInfo | None:
        if info is None:
            return None
        return ObjectInfo(info.key[len(self.prefix):], info.size, info.etag, info.last_modified, info.sha256, info.operation_id, info.metadata)

    def head(self, key):
        return self._strip(self.inner.head(self._k(key)))

    def list(self, prefix: str = "", *, page_size: int = 1000) -> Iterator[ObjectInfo]:
        for info in self.inner.list(self.prefix + prefix, page_size=page_size):
            yield self._strip(info)

    def open_read(self, key) -> BinaryIO:
        return self.inner.open_read(self._k(key))

    def read_range(self, key, start, end):
        return self.inner.read_range(self._k(key), start, end)

    def put_bytes(self, key, data, **kw):
        return self._strip(self.inner.put_bytes(self._k(key), data, **kw))

    def upload_file(self, key, path, **kw):
        return self._strip(self.inner.upload_file(self._k(key), path, **kw))


@dataclass
class Step:
    name: str
    status: str
    detail: str = ""


def run_smoke(store: ObjectStore, *, run_id: str | None = None, multipart_bytes: int = 11 * 1024 * 1024) -> list[Step]:
    run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    s = PrefixedStore(store, f"smoke-tests/{run_id}/")
    steps: list[Step] = []

    def step(name: str, fn) -> None:
        try:
            detail = fn() or ""
            steps.append(Step(name, "PASS", str(detail)))
        except StorageError as exc:  # yapılandırılmış ve gizli bilgi içermeyen hata
            steps.append(Step(name, "FAIL", str(exc)))
        except AssertionError as exc:
            steps.append(Step(name, "FAIL", f"assertion:{exc}"))

    payload = secrets.token_bytes(4096)

    def create_only():
        info = s.put_bytes("objects/a.bin", payload, create_only=True, operation_id=f"{run_id}-a")
        head = s.head("objects/a.bin")
        assert head is not None and head.sha256 == hashlib.sha256(payload).hexdigest() and head.operation_id == f"{run_id}-a"
        return f"etag={info.etag}"

    def collision():
        try:
            s.put_bytes("objects/a.bin", b"other", create_only=True, operation_id=f"{run_id}-b")
        except AlreadyExists:
            return "412 → AlreadyExists"
        raise AssertionError("second create-only write was accepted")

    def idempotent():
        s.put_bytes("objects/a.bin", payload, create_only=True, operation_id=f"{run_id}-a")
        return "same operation + same SHA-256 reconciled"

    def conditional_update():
        v1 = s.put_bytes("state/pointer.json", b"1", create_only=True, operation_id=f"{run_id}-p1")
        s.put_bytes("state/pointer.json", b"2", if_match=v1.etag, operation_id=f"{run_id}-p2")
        try:
            s.put_bytes("state/pointer.json", b"3", if_match=v1.etag, operation_id=f"{run_id}-p3")
        except PreconditionFailed:
            return "If-Match accepted current ETag, rejected stale ETag (412)"
        raise AssertionError("stale If-Match write was accepted")

    def range_read():
        assert s.read_range("objects/a.bin", 100, 199) == payload[100:200]
        with s.open_read("objects/a.bin") as handle:
            assert handle.read() == payload
        return "bytes=100-199"

    def pagination():
        for i in range(3):
            s.put_bytes(f"page/{i}.txt", str(i).encode(), create_only=True, operation_id=f"{run_id}-l{i}")
        keys = [i.key for i in s.list("page/", page_size=1)]
        assert keys == ["page/0.txt", "page/1.txt", "page/2.txt"], keys
        return "3 pages of 1"

    with tempfile.TemporaryDirectory(prefix="thermalis-smoke-") as tmp:
        big = Path(tmp) / "multipart.bin"
        with big.open("wb") as handle:
            handle.write(os.urandom(multipart_bytes))
        big_sha = hashlib.sha256(big.read_bytes()).hexdigest()

        def multipart():
            s.upload_file("objects/multipart.bin", big, expected_sha256=big_sha, operation_id=f"{run_id}-m1")
            result = s.verify_object("objects/multipart.bin", big_sha, expected_size=multipart_bytes)
            assert result.ok, result.problem
            return f"{multipart_bytes} bytes, full SHA-256 read-back OK"

        def multipart_collision():
            try:
                s.upload_file("objects/multipart.bin", big, expected_sha256=big_sha, operation_id=f"{run_id}-m2")
            except AlreadyExists:
                return "CompleteMultipartUpload If-None-Match:* → 412 → AlreadyExists"
            raise AssertionError("second multipart completion was accepted")

        def publication():
            rec = {n: json.dumps({"synthetic": True, "record": n}).encode() for n in ("version.json", "quality.json", "integrity.json")}
            f = Path(tmp) / "data.bin"
            f.write_bytes(secrets.token_bytes(2048))
            files = [ReleaseFile("data/data.bin", f, hashlib.sha256(f.read_bytes()).hexdigest(), 2048)]
            ds = "THERMALIS_DATA/SMOKE_SYNTHETIC/TEST"
            publish_release(s, dataset_id=ds, version="0.0.1", access_class="SYNTHETIC", files=files, records=rec,
                            operation_id=f"{run_id}-rel", policy=PublicationPolicy(allow_cloud_publication=True))
            s.put_bytes("releases/SMOKE_SYNTHETIC/TEST/0.0.2/records/version.json", b"{}", create_only=True,
                        operation_id=f"{run_id}-half")  # işaretsiz yarım sürüm
            visible = [p for p, c, problem in committed_releases(s) if c]
            assert visible == ["releases/SMOKE_SYNTHETIC/TEST/0.0.1/"], visible
            state = inspect_release(s, "releases/SMOKE_SYNTHETIC/TEST/0.0.2/")
            assert state.status == "INCOMPLETE", state
            assert inspect_release(s, "releases/SMOKE_SYNTHETIC/TEST/0.0.1/", full=True).status == "COMMITTED"
            return f"committed 0.0.1 visible; incomplete 0.0.2 invisible ({COMMIT_NAME} is authoritative)"

        for name, fn in (("create_only_put", create_only), ("create_only_collision_412", collision),
                         ("ambiguous_retry_reconciliation", idempotent), ("conditional_update_if_match", conditional_update),
                         ("streaming_and_range_read", range_read), ("paginated_listing", pagination),
                         ("multipart_upload_verified", multipart), ("multipart_completion_collision_412", multipart_collision),
                         ("publication_protocol", publication)):
            step(name, fn)
    return steps
