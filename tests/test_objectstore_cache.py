"""Sınırlı, SHA-256 adresli yerel önbellek."""

from __future__ import annotations

import hashlib
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from conftest import _build_gdb
from thermalis.objectstore.cache import CacheFull, CacheItem, ObjectCache
from thermalis.objectstore.errors import ChecksumMismatch, InvalidKey
from thermalis.objectstore.local import LocalBackend

MIB = 1024 * 1024


class CountingStore(LocalBackend):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.reads = 0

    def open_read(self, key):
        self.reads += 1
        return super().open_read(key)


def _put(store, key: str, data: bytes) -> tuple[str, int]:
    store.put_bytes(key, data, create_only=True, operation_id="op")
    return hashlib.sha256(data).hexdigest(), len(data)


@pytest.fixture()
def store(tmp_path: Path) -> CountingStore:
    return CountingStore(tmp_path / "remote")


def test_fetch_verifies_and_reuses(store, tmp_path: Path) -> None:
    cache = ObjectCache(tmp_path / "cache", 10 * MIB)
    sha, size = _put(store, "a/x.bin", b"x" * 1000)
    path = cache.fetch(store, "a/x.bin", sha, size)
    assert path.read_bytes() == b"x" * 1000 and not os.access(path, os.W_OK)  # salt okunur
    cache.fetch(store, "a/x.bin", sha, size)
    assert store.reads == 1  # ikinci erişim önbellekten (metadata denetimi)
    assert cache.contains(sha, size) and cache.verify(sha)


def test_checksum_failure_never_enters_cache(store, tmp_path: Path) -> None:
    cache = ObjectCache(tmp_path / "cache", 10 * MIB)
    _put(store, "a/x.bin", b"real")
    wrong = hashlib.sha256(b"expected").hexdigest()
    with pytest.raises(ChecksumMismatch):
        cache.fetch(store, "a/x.bin", wrong, 4)
    assert not cache.contains(wrong) and not list((cache.root / "tmp").iterdir())  # yarım dosya kalmadı


def test_lru_eviction_respects_capacity_and_recent_use(store, tmp_path: Path) -> None:
    cache = ObjectCache(tmp_path / "cache", 3 * MIB)
    objs = {n: _put(store, f"o/{n}", bytes([n]) * MIB) for n in range(4)}
    for n in range(3):
        cache.fetch(store, f"o/{n}", *objs[n])
        time.sleep(0.02)
    old = time.time() - 100
    for n, (sha, _) in objs.items():
        if n < 3:
            os.utime(cache.blob_path(sha), (old + n, old + n))
    cache.fetch(store, "o/0", *objs[0])  # 0 yeniden kullanıldı → en eski artık 1
    cache.fetch(store, "o/3", *objs[3])
    assert cache.contains(objs[0][0]) and not cache.contains(objs[1][0]) and cache.contains(objs[2][0])
    assert cache.usage() <= 3 * MIB


def test_leased_entries_are_not_evicted(store, tmp_path: Path) -> None:
    cache = ObjectCache(tmp_path / "cache", 2 * MIB)
    a = _put(store, "o/a", b"a" * MIB)
    b = _put(store, "o/b", b"b" * MIB)
    c = _put(store, "o/c", b"c" * MIB)
    cache.fetch(store, "o/a", *a)
    cache.fetch(store, "o/b", *b)
    with cache.lease(a[0]), cache.lease(b[0]):
        with pytest.raises(CacheFull):
            cache.fetch(store, "o/c", *c)
        assert cache.contains(a[0]) and cache.contains(b[0])
    with cache.lease(a[0]):
        cache.fetch(store, "o/c", *c)
    assert cache.contains(a[0]) and not cache.contains(b[0])
    big = _put(store, "o/big", b"z" * (3 * MIB))
    with pytest.raises(CacheFull):
        cache.fetch(store, "o/big", *big)


def test_corruption_is_detected_and_recovered(store, tmp_path: Path) -> None:
    cache = ObjectCache(tmp_path / "cache", 10 * MIB)
    sha, size = _put(store, "a/x.bin", b"good bytes")
    path = cache.fetch(store, "a/x.bin", sha, size)
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    path.write_bytes(b"evil bytes")  # aynı boyut: metadata denetimi yakalamaz
    assert cache.contains(sha, size) is True
    fixed = cache.fetch(store, "a/x.bin", sha, size, full_verify=True)
    assert fixed.read_bytes() == b"good bytes" and store.reads == 2


def test_incomplete_downloads_are_cleaned_on_start(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    (root / "tmp").mkdir(parents=True)
    stale = root / "tmp" / ("a" * 64 + ".x.part")
    stale.write_bytes(b"partial")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    fresh = root / "tmp" / ("b" * 64 + ".y.part")
    fresh.write_bytes(b"in progress")
    ObjectCache(root, 10 * MIB)
    assert not stale.exists() and fresh.exists()  # sürmekte olan indirmeye dokunulmaz


def test_cache_must_not_overlap_protected_data(tmp_path: Path) -> None:
    data_root = tmp_path / "THERMALIS_DATA"
    data_root.mkdir()
    with pytest.raises(InvalidKey):
        ObjectCache(data_root / "cache", 10 * MIB, protected_roots=[data_root])
    with pytest.raises(InvalidKey):
        ObjectCache(tmp_path, 10 * MIB, protected_roots=[data_root])  # önbellek veri kökünü kapsayamaz
    ObjectCache(tmp_path / "cache", 10 * MIB, protected_roots=[data_root])


def test_concurrent_readers_get_one_verified_copy(store, tmp_path: Path) -> None:
    cache = ObjectCache(tmp_path / "cache", 10 * MIB)
    data = os.urandom(2 * MIB)
    sha, size = _put(store, "a/x.bin", data)
    results: list[bytes] = []
    errors: list[BaseException] = []

    def reader() -> None:
        try:
            results.append(cache.fetch(store, "a/x.bin", sha, size).read_bytes())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and all(r == data for r in results) and len(results) == 8
    assert store.reads == 1 and cache.usage() == size


def test_file_geodatabase_materializes_with_structure_and_opens_read_only(store, tmp_path: Path) -> None:
    from thermalis.data.vector_inspect import layer_facts

    gdb = tmp_path / "src" / "Admin.gdb"
    gdb.parent.mkdir()
    _build_gdb(gdb, None)
    items = []
    for path in sorted(p for p in gdb.rglob("*") if p.is_file()):
        rel = "Admin.gdb/" + path.relative_to(gdb).as_posix()
        sha, size = _put(store, "releases/X/payload/" + rel, path.read_bytes())
        items.append(CacheItem("releases/X/payload/" + rel, rel, sha, size))
    cache = ObjectCache(tmp_path / "cache", 50 * MIB)
    expected = layer_facts(gdb, "Neighborhoods")["content_id"]
    with cache.materialize(store, "TT01", items) as folder:
        assert layer_facts(folder / "Admin.gdb", "Neighborhoods")["content_id"] == expected
        with pytest.raises(CacheFull):  # kullanımdaki içerik tahliye edilemez
            big = _put(store, "o/big", b"z" * (50 * MIB))
            cache.fetch(store, "o/big", *big)
    with cache.materialize(store, "TT01", items) as again:
        assert again == folder  # aynı içerik → aynı klasör, yeniden kullanılır
    with pytest.raises(InvalidKey):
        with cache.materialize(store, "TT01", [CacheItem(items[0].key, "../escape", items[0].sha256, items[0].size)]):
            pass
