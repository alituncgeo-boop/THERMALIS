"""Yerel arka uca özgü davranış ve mevcut kayıt akışının depo arayüzüyle uyumluluğu (Faz 02 kabul kapısı)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from conftest import build_package, write_ready_marker
from thermalis.data import catalog
from thermalis.data.integrity import ManifestEntry, verify_store, verify_tree
from thermalis.data.ingest import run_ingest
from thermalis.data.register import verify_registered
from thermalis.objectstore.errors import InvalidKey
from thermalis.objectstore.local import STORE_DIR, LocalBackend

DS = "THERMALIS_DATA/TT01_ADMIN_BOUNDARIES/ISTANBUL"


def test_existing_files_are_readable_without_metadata_and_nothing_is_written(tmp_path: Path) -> None:
    root = tmp_path / "data"
    (root / "datasets" / "A").mkdir(parents=True)
    (root / "datasets" / "A" / "version.json").write_text("{}", encoding="utf-8")
    store = LocalBackend(root)
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    info = store.head("datasets/A/version.json")
    assert info is not None and info.sha256 is None and info.etag.startswith('"st-')  # özet bilinmiyor: uydurulmaz
    assert [i.key for i in store.list("datasets/")] == ["datasets/A/version.json"]
    assert store.verify_object("datasets/A/version.json", hashlib.sha256(b"{}").hexdigest()).ok
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == before  # okuma iz bırakmaz
    assert not (root / STORE_DIR).exists()


def test_internal_store_files_are_never_listed(tmp_path: Path) -> None:
    store = LocalBackend(tmp_path)
    store.put_bytes("x/a.txt", b"a", create_only=True, operation_id="op")
    assert (tmp_path / STORE_DIR).is_dir()
    assert [i.key for i in store.list()] == ["x/a.txt"]


def test_externally_modified_file_loses_recorded_sha(tmp_path: Path) -> None:
    store = LocalBackend(tmp_path)
    store.put_bytes("x/a.txt", b"aaaa", create_only=True, operation_id="op")
    path = tmp_path / "x" / "a.txt"
    os.chmod(path, 0o600)
    path.write_bytes(b"bbbbbb")
    info = store.head("x/a.txt")
    assert info is not None and info.sha256 is None  # yan dosya artık tutarsız: kayıtlı özet kullanılmaz


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    try:
        os.symlink(outside, root / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("bu ortamda sembolik bağlantı oluşturulamıyor")
    store = LocalBackend(root)
    with pytest.raises(InvalidKey):
        store.head("link/secret.txt")
    assert [i.key for i in store.list()] == []  # bağlantı izlenmez


def test_verify_store_matches_verify_tree(tmp_path: Path) -> None:
    root = tmp_path / "payload"
    (root / "a").mkdir(parents=True)
    (root / "a" / "one.bin").write_bytes(b"1" * 10)
    (root / "a" / "two.bin").write_bytes(b"2" * 10)
    (root / "extra.txt").write_bytes(b"e")
    (root / "x.lock").write_bytes(b"")
    entries = [ManifestEntry("a/one.bin", 10, hashlib.sha256(b"1" * 10).hexdigest()),
               ManifestEntry("a/two.bin", 10, "0" * 64), ManifestEntry("a/gone.bin", 1, "0" * 64)]
    old = verify_tree(root, entries).as_dict()
    new = verify_store(LocalBackend(tmp_path), "payload/", entries).as_dict()
    assert new == old
    assert {p["problem"] for p in new["problems"]} == {"SHA256_MISMATCH", "MISSING", "UNEXPECTED_FILE"}


def test_registered_version_reverification_goes_through_store(tmp_path: Path, data_root: Path) -> None:
    incoming = tmp_path / "IN"
    incoming.mkdir()
    write_ready_marker(build_package(incoming / "TT01_v1"))
    (result,) = run_ingest(incoming, data_root, settle_seconds=0).results
    assert result.technical == "PASS"
    assert catalog.version_prefix(DS, "1.0.0-rc1") == "datasets/TT01_ADMIN_BOUNDARIES/ISTANBUL/1.0.0-rc1/"
    assert [f.name for f in catalog.version_record_folders(data_root)] == ["1.0.0-rc1"]
    checks = verify_registered(data_root, DS, "1.0.0-rc1")
    assert all(c.status == "PASS" for c in checks)
    folder = catalog.version_dir(data_root, DS, "1.0.0-rc1")
    victim = next(p for p in (folder / "payload").rglob("*.gdbtable") if p.is_file())  # manifestte özetlenen dosya
    os.chmod(victim, 0o600)
    victim.write_bytes(victim.read_bytes() + b" ")
    failed = verify_registered(data_root, DS, "1.0.0-rc1")
    assert failed[0].status == "FAIL" and "SIZE_MISMATCH" in failed[0].detail
    assert not (catalog.data_dir(data_root) / STORE_DIR).exists()  # yeniden doğrulama depo metadata'sı yazmaz
