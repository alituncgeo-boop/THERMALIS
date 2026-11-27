import hashlib
import json
from pathlib import Path

import pytest

from thermalis.cloud import layout
from thermalis.cloud.access import materialize_asset, materialize_member
from thermalis.cloud.catalog_sync import mirror_folder, sync_catalog
from thermalis.cloud.direct import DirectRefused, plan_collection, publish_collection, verify_collection
from thermalis.data import contract
from thermalis.data.collection import CollectionError, open_source_package
from thermalis.objectstore.cache import ObjectCache
from thermalis.objectstore.errors import InvalidKey, StorageError
from thermalis.objectstore.local import LocalBackend
from thermalis.objectstore.prefixed import namespaced
from thermalis.objectstore.publication import read_commit, release_prefix

DATASET = "THERMALIS_DATA/DIRECTORY_TEST/CITY"
VERSION = "1.0.0-rc1"
LAYER = DATASET + "/SOURCE_FILES"


def sha(body):
    return hashlib.sha256(body).hexdigest()


def write_manifest(root, manifest):
    raw = json.dumps(manifest).encode("utf-8")
    (root / "MANIFEST.json").write_bytes(raw)
    (root / "MANIFEST_SHA256.txt").write_text(f"{sha(raw)}  MANIFEST.json\n", encoding="utf-8")
    return sha(raw)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "sources"
    files = {"nested/opaque.zip/file.nc": b"native bytes", "tiles/image.tif": b"opaque",
             "RELEASE_STATUS.json": b'{"source_qa":"QA_CONDITIONAL","analysis_readiness":"NOT_READY"}',
             "LICENSE_AND_ACCESS.md": b"Source citation required.\n", "provenance/streams.json": b'{"streams":[]}'}
    for rel, value in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(value)
    (root / "nested/empty").mkdir()
    manifest = {"format": "thermalis-directory-manifest-v1", "release_id": "DIRECTORY_TEST_v1.0.0-rc1",
                "files": [{"path": p, "bytes": len(v), "sha256": sha(v)} for p, v in files.items()],
                "source_directories": ["nested/empty"], "source_ntfs_streams": "provenance/streams.json"}
    declaration = {"schema": "thermalis.package-declaration/1", "package_id": manifest["release_id"],
                   "manifest_sha256": write_manifest(root, manifest), "dataset_id": DATASET, "version": VERSION,
                   "access_class": "PUBLIC", "declared_by": "synthetic test", "declared_at": "2026-11-27T00:00:00Z",
                   "source": {"product": "fixture", "citation": "synthetic only"},
                   "quality": {"source_status_ref": "RELEASE_STATUS.json#source_qa"},
                   "directory": {"documents": ["LICENSE_AND_ACCESS.md", "RELEASE_STATUS.json"]}}
    return root, files, manifest, declaration


def plan(source, tmp_path):
    return plan_collection(source[0], source[3], data_root=tmp_path / "registry", work_root=tmp_path / "staging",
                           provider="r2", bucket="fixture")


def snapshot(root):
    return {p.relative_to(root).as_posix(): (p.stat().st_mtime_ns, sha(p.read_bytes())) for p in root.rglob("*") if p.is_file()}


def test_publication_mirror_and_raw_materialization(source, tmp_path):
    root, files, _, _ = source
    before = snapshot(root)
    first, repeat = plan(source, tmp_path), plan(source, tmp_path)
    assert repeat.built.reused and repeat.records == first.records
    assert set(f.relpath for f in first.files) == set(before)
    assert all(f.path.is_relative_to(root) for f in first.files)
    assert json.loads(first.records["quality.json"])["status"] == "QA_CONDITIONAL"
    info = json.loads(first.records["directory.json"])
    assert "nested/empty" in info["directories"] and info["analysis_ready"] is False
    assert info["named_stream_inventory"] == "provenance/streams.json"
    store = namespaced(LocalBackend(tmp_path / "objects"), "synthetic/")
    assert publish_collection(store, first, machine="one")["status"] == "PUBLISHED"
    assert verify_collection(store, first, full=True)["status"] == "PASS"
    prefix = release_prefix(DATASET, VERSION)
    assert read_commit(store, prefix)["verification"] == "full_sha256_read_back"
    with store.open_read(layout.catalog_entry_key(DATASET, VERSION)) as stream:
        assert json.load(stream)["record_kind"] == "directory_source"
    assert publish_collection(store, repeat, machine="one")["status"] == "ALREADY_PUBLISHED"
    data = tmp_path / "registry"
    result = sync_catalog(store, data, provider="r2", bucket="fixture", namespace="synthetic/")
    assert result.status == "ok" and not result.problems
    folder = mirror_folder(data, DATASET, VERSION)
    assert (folder / "payload/LICENSE_AND_ACCESS.md").read_bytes() == files["LICENSE_AND_ACCESS.md"]
    assert not (folder / "payload/tiles/image.tif").exists()
    assert contract.descriptor(data, LAYER, VERSION)["scientific"]["record_kind"] == "directory_source"
    access = contract.access(data, LAYER, VERSION)
    assert access["storage"]["analysis_ready"] is False
    assert access["storage"]["raw_file_selector"]["item"] == "files"
    assert "gdal_path" not in access["storage"]
    cache = ObjectCache(tmp_path / "cache", 2 * 1024**3)
    inventory = materialize_asset(data, store, cache, LAYER, VERSION)
    assert Path(inventory["path"]).read_bytes() == (root / "MANIFEST.json").read_bytes()
    rel = "nested/opaque.zip/file.nc"
    raw = materialize_member(data, store, cache, LAYER, VERSION, "files", rel)
    assert Path(raw["path"]).read_bytes() == files[rel] and raw["files"] == 1
    assert raw["verification"] == "sha256_per_object_against_commit"
    with pytest.raises(StorageError, match="asset_not_in_commit"):
        materialize_member(data, store, cache, LAYER, VERSION, "files", "nested")
    with pytest.raises(InvalidKey, match="empty_or_relative_segment"):
        materialize_member(data, store, cache, LAYER, VERSION, "files", "../outside")
    assert snapshot(root) == before


@pytest.mark.parametrize("change, code", [
    ("bad_anchor", "directory_manifest_anchor_mismatch"), ("missing_anchor", "directory_manifest_controls_missing"),
    ("traversal", "manifest_entry_invalid"), ("case_alias", "manifest_duplicate_path"),
    ("unlisted", "package_file_not_in_manifest"), ("missing_directory", "directory_manifest_directory_mismatch"),
])
def test_invalid_directory_controls(source, change, code):
    root, _, manifest, _ = source
    if change == "bad_anchor":
        (root / "MANIFEST_SHA256.txt").write_text("0" * 64, encoding="utf-8")
    elif change == "missing_anchor":
        (root / "MANIFEST_SHA256.txt").unlink()
    elif change == "unlisted":
        (root / "unlisted.dat").write_bytes(b"x")
    else:
        if change == "traversal":
            manifest["files"][0]["path"] = "../release.nc"
        elif change == "case_alias":
            manifest["files"].append({**manifest["files"][0], "path": manifest["files"][0]["path"].upper()})
        else:
            manifest["source_directories"].append("missing/empty")
        write_manifest(root, manifest)
    with pytest.raises(CollectionError, match=code):
        open_source_package(root)


def test_content_mismatch_and_late_source_removal(source, tmp_path):
    p = source[0] / "tiles/image.tif"
    expected = p.read_bytes()
    p.write_bytes(b"n" * len(expected))
    with pytest.raises(DirectRefused, match="sha256_mismatch"):
        plan(source, tmp_path)
    p.write_bytes(expected)
    checked = plan(source, tmp_path)
    p.unlink()
    store = LocalBackend(tmp_path / "objects")
    with pytest.raises(DirectRefused, match="source_changed_since_indexing"):
        publish_collection(store, checked, machine="one")
    assert list(store.list()) == []


def test_link_refused_before_external_read(source, tmp_path):
    outside = tmp_path / "outside"
    outside.write_bytes(b"x")
    try:
        (source[0] / "link").symlink_to(outside)
    except OSError:
        pytest.skip("no symlink privilege")
    with pytest.raises(CollectionError, match="directory_link_unsupported"):
        open_source_package(source[0])
