"""Nesne deposu sözleşmesi: her arka uç (yerel + S3/R2 sahte istemcisi) aynı testlerden geçer.

Sahte S3 istemcisi R2'nin belgelenmiş koşullu işlem davranışını taklit eder; bu testler gerçek R2
uyumluluğunu KANITLAMAZ. Gerçek R2 için elle çalıştırılan ``scripts/r2_smoke_test.py`` vardır.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pytest

from thermalis.objectstore.base import CancelToken
from thermalis.objectstore.errors import (AlreadyExists, Cancelled, CapabilityNotSupported, ChecksumMismatch, InvalidKey,
                                          NotFound, PreconditionFailed, RangeNotSatisfiable)
from thermalis.objectstore.local import LocalBackend

BACKENDS = ["local", "s3"]


def _make(kind: str, tmp_path: Path):
    if kind == "local":
        store = LocalBackend(tmp_path / "store")

        def tamper(key: str, data: bytes) -> None:
            path = store.root.joinpath(*key.split("/"))
            path.write_bytes(data)

        return store, tamper
    from s3fake import make_s3_backend

    store, server = make_s3_backend(tmp_path)

    def tamper(key: str, data: bytes) -> None:
        server.objects[key]["body"] = data  # metadata/ETag aynı kalır: yalnız tam doğrulama yakalar

    return store, tamper


@pytest.fixture(params=BACKENDS)
def backend(request, tmp_path: Path):
    return _make(request.param, tmp_path)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_create_only_write_and_metadata(backend) -> None:
    store, _ = backend
    info = store.put_bytes("a/b/c.json", b'{"x": 1}', create_only=True, operation_id="op-1", metadata={"role": "test"})
    head = store.head("a/b/c.json")
    assert head is not None and store.exists("a/b/c.json") and not store.exists("a/b/missing.json")
    assert (head.size, head.sha256, head.operation_id) == (8, _sha(b'{"x": 1}'), "op-1")
    assert head.metadata.get("role") == "test" and head.etag == info.etag and head.etag
    assert store.capabilities.delete is False  # uygulama arayüzünde silme yok


def test_create_only_collision_and_idempotent_retry(backend) -> None:
    store, _ = backend
    first = store.put_bytes("k/x.bin", b"one", create_only=True, operation_id="op-1")
    again = store.put_bytes("k/x.bin", b"one", create_only=True, operation_id="op-1")  # belirsiz yazımın tekrarı
    assert again.etag == first.etag and again.operation_id == "op-1"
    with pytest.raises(AlreadyExists):
        store.put_bytes("k/x.bin", b"two", create_only=True, operation_id="op-2")
    with pytest.raises(AlreadyExists):
        store.put_bytes("k/x.bin", b"one", create_only=True, operation_id="op-other")  # başka işlem: çakışma
    with store.open_read("k/x.bin") as handle:
        assert handle.read() == b"one"  # üzerine yazılmadı


def test_unconditional_write_is_refused(backend) -> None:
    store, _ = backend
    with pytest.raises(CapabilityNotSupported):
        store.put_bytes("k/y.bin", b"z")
    with pytest.raises(CapabilityNotSupported):
        store.put_bytes("k/y.bin", b"z", create_only=True, if_match='"x"')


def test_conditional_update_with_etag(backend) -> None:
    store, _ = backend
    v1 = store.put_bytes("state/dataset.json", b"v1", create_only=True, operation_id="op-1")
    v2 = store.put_bytes("state/dataset.json", b"v2", if_match=v1.etag, operation_id="op-2")
    assert v2.etag != v1.etag
    with pytest.raises(PreconditionFailed):
        store.put_bytes("state/dataset.json", b"v3", if_match=v1.etag, operation_id="op-3")  # eski sürüm
    with pytest.raises(PreconditionFailed):
        store.put_bytes("state/none.json", b"v1", if_match=v1.etag)  # nesne yok
    with store.open_read("state/dataset.json") as handle:
        assert handle.read() == b"v2"


def test_concurrent_conditional_updates_have_one_winner(backend) -> None:
    store, _ = backend
    base = store.put_bytes("state/counter.json", b"0", create_only=True, operation_id="op-0")
    outcomes: list[str] = []

    def writer(n: int) -> None:
        try:
            store.put_bytes("state/counter.json", str(n).encode(), if_match=base.etag, operation_id=f"op-{n}")
            outcomes.append("won")
        except PreconditionFailed:
            outcomes.append("lost")

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(1, 7)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("won") == 1 and outcomes.count("lost") == 5


def test_listing_is_sorted_paginated_and_prefix_scoped(backend) -> None:
    store, _ = backend
    for name in ("p/e", "p/a", "p/c/d", "p/b", "q/z", "p/ç"):
        store.put_bytes(name, name.encode(), create_only=True, operation_id="op")
    keys = [i.key for i in store.list("p/", page_size=2)]
    assert keys == sorted(["p/a", "p/b", "p/c/d", "p/e", "p/ç"], key=lambda k: k.encode("utf-8"))
    assert [i.key for i in store.list("p/c/")] == ["p/c/d"]
    assert list(store.list("nothing/")) == []
    with pytest.raises(InvalidKey):
        list(store.list("p"))  # önek '/' ile bitmeli: dosya sistemi taklidi yok


def test_streaming_and_range_reads(backend) -> None:
    store, _ = backend
    data = bytes(range(256)) * 40
    store.put_bytes("r/blob", data, create_only=True, operation_id="op")
    with store.open_read("r/blob") as handle:
        assert handle.read(10) == data[:10] and handle.read() == data[10:]
    assert store.read_range("r/blob", 100, 199) == data[100:200]
    assert store.read_range("r/blob", len(data) - 5, len(data) + 100) == data[-5:]  # HTTP gibi uca kırpılır
    with pytest.raises(RangeNotSatisfiable):
        store.read_range("r/blob", len(data), len(data) + 1)
    with pytest.raises(RangeNotSatisfiable):
        store.read_range("r/blob", 5, 4)
    with pytest.raises(NotFound):
        store.read_range("r/missing", 0, 1)
    with pytest.raises(NotFound):
        store.open_read("r/missing")


def test_upload_file_verifies_source_before_publication(backend, tmp_path: Path) -> None:
    store, _ = backend
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload" * 1000)
    good = _sha(src.read_bytes())
    info = store.upload_file("u/ok.bin", src, expected_sha256=good, operation_id="op-u1")
    assert info.sha256 == good and store.verify_object("u/ok.bin", good).ok
    with pytest.raises(ChecksumMismatch):
        store.upload_file("u/bad.bin", src, expected_sha256="0" * 64, operation_id="op-u2")
    assert not store.exists("u/bad.bin")  # doğrulanamayan nesne görünür olmaz


def test_metadata_check_is_not_full_verification(backend) -> None:
    store, tamper = backend
    data = b"scientific bytes"
    store.put_bytes("v/x", data, create_only=True, operation_id="op")
    tamper("v/x", b"scientific BYTES")  # aynı boyut
    meta = store.check_metadata("v/x", expected_size=len(data), expected_sha256=_sha(data))
    full = store.verify_object("v/x", _sha(data), expected_size=len(data))
    assert meta.level == "metadata" and full.level == "full"
    assert full.ok is False and full.problem == "SHA256_MISMATCH"
    with pytest.raises(ChecksumMismatch):
        store.require_verified("v/x", _sha(data))
    assert store.verify_object("v/none", _sha(data)).problem == "MISSING"


@pytest.mark.parametrize("key", ["../x", "a/../b", "/abs", "a/", "a//b", "a/./b", "C:/x", "a\\b", "CON", "a/nul.txt",
                                 "a/b.", "a/b ", "a\x00b", "a\nb", "a:b", ".thermalis-store/x", "x" * 1100, ""])
def test_invalid_keys_are_rejected(backend, key: str) -> None:
    store, _ = backend
    with pytest.raises(InvalidKey):
        store.put_bytes(key, b"x", create_only=True, operation_id="op")
    with pytest.raises(InvalidKey):
        store.head(key)


def test_cancellation_stops_long_operations(backend, tmp_path: Path) -> None:
    store, _ = backend
    src = tmp_path / "big.bin"
    src.write_bytes(b"x" * (9 * 1024 * 1024))
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        store.upload_file("c/big.bin", src, expected_sha256=_sha(src.read_bytes()), operation_id="op-c", cancel=token)
    assert not store.exists("c/big.bin")
