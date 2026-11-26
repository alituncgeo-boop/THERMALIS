"""Raster koleksiyonları: genel TAR/COG desteği, Landsat yorumlayıcısı, paket dışı beyan, doğrudan yayın,
aralıklı erişim, bulut kabulü ön denetimi ve yenilenebilir talepler (sentetik veri; ağ yok)."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from rasterfake import STATUS, SCALE, build_collection_package, declaration_for, declaration_spec, scene_members, write_tar
from s3fake import make_s3_backend
from thermalis.cloud import layout
from thermalis.cloud.claims import Busy, ClaimLost, LocalLock, RenewableClaim
from thermalis.cloud.direct import DirectRefused, plan_collection, publish_collection, verify_collection
from thermalis.data import declaration as decl
from thermalis.data.collection import CollectionError, build_records, load_source_package, subset_package
from thermalis.data.formats import landsat
from thermalis.data.formats.preflight import preflight
from thermalis.data.formats.raster import inspect_raster, read_window
from thermalis.data.formats.tar_index import TarIndexError, index_tar, vsisubfile
from thermalis.objectstore.base import CancelToken
from thermalis.objectstore.errors import Cancelled
from thermalis.objectstore.publication import PublicationPolicy, ReleaseFile, publish_release, read_commit, release_prefix

BUCKET = "thermalis-test-bucket"
DS = "THERMALIS_DATA/SYN02_LST/TESTKENT_180031"
LAYER = f"{DS}/SCENES"


def _tree_stat(root: Path) -> dict[str, tuple[int, int, str]]:
    return {p.relative_to(root).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture()
def coll(tmp_path: Path) -> Path:
    return build_collection_package(tmp_path / "SYN02_LST_v1.0.0-rc1")


# ----------------------------------------------------------------------------- genel TAR dizini

@pytest.mark.parametrize("fmt", [tarfile.USTAR_FORMAT, tarfile.GNU_FORMAT, tarfile.PAX_FORMAT])
def test_tar_index_matches_tarfile_offsets_and_hashes(tmp_path: Path, fmt) -> None:
    # 100 karakterden uzun ad: ustar'da önek alanına, GNU'da uzun ad kaydına, PAX'ta "path" kaydına düşer.
    members = {"short.txt": b"abc", "klasor_" * 8 + "/" + "uzun_ad_" * 11 + ".bin": bytes(range(256)) * 41,
               "ç/ğ/ü.json": b'{"x": 1}', "empty": b""}
    path = tmp_path / "t.tar"
    write_tar(path, members, fmt=fmt)
    with path.open("rb") as handle:
        idx = index_tar(handle)
    with tarfile.open(path, "r:") as t:
        ref = {m.name: (m.offset_data, m.size) for m in t.getmembers()}
    assert {m.name: (m.offset, m.size) for m in idx.files()} == ref
    assert idx.archive_sha256 == hashlib.sha256(path.read_bytes()).hexdigest() and idx.archive_size == path.stat().st_size
    for m in idx.files():
        assert m.sha256 == hashlib.sha256(members[m.name]).hexdigest()
        with path.open("rb") as handle:  # konum doğrudan okumaya yeter (aralıklı okuma temeli)
            handle.seek(m.offset)
            assert handle.read(m.size) == members[m.name]


def test_tar_index_rejects_compressed_and_non_tar_and_truncated(tmp_path: Path) -> None:
    path = tmp_path / "t.tar"
    write_tar(path, {"x": b"1" * 2000})
    with pytest.raises(TarIndexError) as e:
        index_tar(io.BytesIO(gzip.compress(path.read_bytes())))
    assert e.value.code == "compressed_tar_not_range_readable"
    with pytest.raises(TarIndexError) as e:
        index_tar(io.BytesIO(b"not a tar" * 100))
    assert e.value.code == "not_a_tar"
    with pytest.raises(TarIndexError) as e:
        index_tar(io.BytesIO(path.read_bytes()[:1000]))
    assert e.value.code == "truncated_archive"


def test_tar_index_flags_unsafe_member_names(tmp_path: Path) -> None:
    path = tmp_path / "t.tar"
    write_tar(path, {"../evil": b"x", "ok": b"y"})
    with path.open("rb") as handle:
        idx = index_tar(handle)
    assert {m.name: m.safe_name for m in idx.members} == {"../evil": False, "ok": True}


# ----------------------------------------------------------------------------- raster + yorumlayıcı

def test_raster_inspection_inside_tar_without_extraction(tmp_path: Path) -> None:
    pid = "LC08_L2SP_180031_20200628_20200912_02_T1"
    members = scene_members(pid, "2020-06-28", "LANDSAT_8", 9.45, seed=1)
    path = tmp_path / "s.tar"
    write_tar(path, members)
    with path.open("rb") as handle:
        idx = index_tar(handle)
    st = idx.member(f"{pid}_ST_B10.TIF")
    info = inspect_raster(vsisubfile(st, path.as_posix()))
    assert info["readable"] and info["crs"]["epsg"] == 32635 and info["resolution"] == [30.0, 30.0]
    assert info["bands"][0]["dtype"] == "UInt16" and info["bands"][0]["nodata"] == 0
    assert info["bands"][0]["embedded_scale"] is None  # gömülü ölçek yok: uydurulmaz
    assert info["cloud_optimized"] is True and info["layout"] == "COG"
    # Ham değerler (DN) değişmeden okunur: arşiv içinden okunan pencere = bağımsız dosyadan okunan pencere.
    alone = tmp_path / "b10.tif"
    alone.write_bytes(members[f"{pid}_ST_B10.TIF"])
    assert read_window(vsisubfile(st, path.as_posix()), 1, 0, 0, 16, 16) == read_window(str(alone), 1, 0, 0, 16, 16)


def test_landsat_interpreter_records_source_declarations_without_applying(tmp_path: Path) -> None:
    pid = "LC09_L2SP_180031_20220629_20230409_02_T1"
    members = scene_members(pid, "2022-06-29", "LANDSAT_9", 26.16, seed=2)
    assert landsat.detect(list(members)) and not landsat.detect(["x.tif"])
    out = landsat.interpret(list(members), lambda n: members[n])
    p = out["properties"]
    assert (p["product_id"], p["platform"], p["acquisition_date"], p["cloud_cover"], p["wrs_path"]) == \
        (pid, "LANDSAT_9", "2022-06-29", 26.16, "180")
    b10 = out["assets"][f"{pid}_ST_B10.TIF"]
    assert b10["scale"] == float(SCALE) and b10["add_offset"] == 149.0 and b10["scale_status"] == "source_declared"
    assert "TEMPERATURE_MULT_BAND_ST_B10" in b10["scale_source"] and b10["unit"] is None
    qa = out["assets"][f"{pid}_ST_QA.TIF"]
    assert qa["scale"] is None and qa["scale_status"] == "not_declared_in_source_metadata" and qa["role"] == "quality"
    assert out["assets"][f"{pid}_QA_PIXEL.TIF"]["stac"]["classification:bitfields"]


# ----------------------------------------------------------------------------- ön denetim + beyan

def test_preflight_routes_and_adapter_required() -> None:
    assert preflight(["PACKAGE/MANIFEST.json", "SOURCE/2020/a.tar"]).route == "raster_collection"
    assert preflight(["x/Admin.gdb/a00000001.gdbtable", "PACKAGE/R.json"]).route == "vector_package"
    assert preflight(["d.tif", "PACKAGE/X.json"]).route == "raster_collection"
    nc = preflight(["a.nc", "PACKAGE/X.json"], driver_available=lambda d: False)
    assert nc.route is None and nc.reasons == ["netcdf_driver_missing_requires_adapter"] and nc.unsupported == ["a.nc"]
    assert preflight(["a.tar.gz"]).reasons == ["compressed_tar_not_range_readable"]
    assert preflight(["a.zip"]).route is None
    assert preflight(["g.gdb/x.gdbtable", "s.tar"]).reasons == ["mixed_vector_and_raster_package_requires_adapter"]
    assert preflight(["PACKAGE/README.md"]).reasons == ["no_data_files"]


def test_quality_mapping_never_upgrades() -> None:
    assert decl.quality_from_source_status(STATUS) == ("QA_CONDITIONAL", "qualified_pass_maps_to_conditional")
    assert decl.quality_from_source_status("SOURCE_QA_PASS")[0] == "QA_PASS"
    assert decl.quality_from_source_status("QA_FAIL_WITH_PASS_PARTS")[0] == "QA_FAIL"
    assert decl.quality_from_source_status("")[0] == "QA_PENDING"
    assert decl.quality_from_source_status("SOMETHING")[0] == "QA_PENDING"


def test_declaration_is_bound_to_manifest_and_cannot_set_quality(coll: Path) -> None:
    d = declaration_for(coll)
    pkg = load_source_package(coll)
    assert d["manifest_sha256"] == pkg.manifest_sha256 and d["package_id"] == "SYN02_LST_v1.0.0-rc1"
    with pytest.raises(decl.DeclarationError) as e:
        decl.validate(d, manifest_sha256="0" * 64)
    assert e.value.code == "declaration_does_not_match_package"
    with pytest.raises(decl.DeclarationError) as e:
        decl.validate({**d, "quality": {**d["quality"], "status": "QA_PASS"}})
    assert e.value.code == "declaration_cannot_set_quality_status"
    with pytest.raises(decl.DeclarationError):
        decl.validate({**d, "access_class": "OPEN"})
    with pytest.raises(decl.DeclarationError):
        decl.validate({**d, "collection": {**d["collection"], "interpreter": "unknown_sensor"}})


def test_declaration_save_is_create_only(coll: Path, data_root: Path) -> None:
    d = declaration_for(coll)
    path = decl.save(data_root, d)
    assert decl.save(data_root, d) == path  # aynı içerik: dokunulmaz
    with pytest.raises(decl.DeclarationError) as e:
        decl.save(data_root, {**d, "version": "1.0.0-rc2"})
    assert e.value.code == "declaration_exists_with_different_content"
    assert decl.find(data_root, d["package_id"], d["manifest_sha256"]) == d


# ----------------------------------------------------------------------------- koleksiyon kaydı

def test_build_records_indexes_scenes_and_preserves_source(coll: Path, tmp_path: Path) -> None:
    before = _tree_stat(coll)
    pkg = load_source_package(coll)
    built = build_records(pkg, declaration_for(coll), tmp_path / "work", ingest_id="test")
    assert _tree_stat(coll) == before  # kaynak paket bayt, boyut ve zaman olarak aynı
    version = json.loads(built.records["version.json"])
    quality = json.loads(built.records["quality.json"])
    col = json.loads(built.records["collection.json"])
    assert version["record_kind"] == "raster_collection" and version["access"]["classification"] == "PUBLIC"
    assert version["layers"]["SCENES"]["feature_count"] == 2 and version["layers"]["SCENES"]["expected_feature_count"] == 2
    assert version["crs"]["factory_code"] == 32635 and version["crs"]["unit"] == "metre"
    assert version["source"]["temporal_coverage"]["first_acquisition"] == "2020-06-28"
    assert quality["status"] == "QA_CONDITIONAL" and quality["source_status"] == STATUS
    assert quality["documented_limitations"] == ["seasonal sampling", "not continuous"]
    assert all(c["value"] == STATUS for c in quality["source_status_cross_checks"])
    assert col["issues"] == [] and built.issues == []
    names = {i["archive"]["path"] for i in col["items"]}
    assert "SOURCE/2020/LC08_L2SP_180031_20200628_20200912_02_T1 (3).tar" in names  # yeniden adlandırma yok
    item = col["items"][0]
    with tarfile.open(coll / item["archive"]["path"], "r:") as t:
        ref = {m.name: (m.offset_data, m.size) for m in t.getmembers()}
    assert {a["member"]: (a["offset"], a["bytes"]) for a in item["assets"]} == ref
    b10 = next(a for a in item["assets"] if a["band_code"] == "ST_B10")
    assert b10["scale"] == float(SCALE) and b10["raster"]["bands"][0]["nodata"] == 0
    assert item["package_properties"]["source_qa_status"] == "QA_COMPLETE"
    integ = json.loads(built.records["integrity.json"])
    assert integ["file_count"] == len(pkg.entries) and integ["members"]["members_hashed"] == sum(len(i["assets"]) for i in col["items"])


def test_records_are_pinned_and_journal_avoids_rereading(coll: Path, tmp_path: Path, monkeypatch) -> None:
    pkg = load_source_package(coll)
    work = tmp_path / "work"
    first = build_records(pkg, declaration_for(coll), work, ingest_id="op")
    import thermalis.data.collection as mod

    def boom(*a, **k):
        raise AssertionError("archive re-read")
    monkeypatch.setattr(mod, "index_tar", boom)
    again = build_records(pkg, declaration_for(coll), work, ingest_id="op")
    assert again.reused and again.records == first.records  # aynı baytlar: yeniden başlatmada aynı kayıt yayımlanır
    (work / "records" / "binding.json").unlink()  # kayıtlar yeniden üretilir ama arşivler günlükten (yeniden okunmaz)
    rebuilt = build_records(pkg, declaration_for(coll), work, ingest_id="op")
    assert not rebuilt.reused and json.loads(rebuilt.records["collection.json"]) == json.loads(first.records["collection.json"])


def test_sha_mismatch_unlisted_file_and_catalog_conflict_are_detected(tmp_path: Path) -> None:
    def corrupt(meta):
        meta["sha256_files"][-1]["sha256"] = "0" * 64
    bad = build_collection_package(tmp_path / "BAD_v1.0.0", manifest_override=corrupt)
    with pytest.raises(CollectionError) as e:
        build_records(load_source_package(bad), declaration_for(bad), tmp_path / "w1", ingest_id="x")
    assert e.value.code == "sha256_mismatch"
    extra = build_collection_package(tmp_path / "EXTRA_v1.0.0")
    (extra / "SOURCE" / "stray.bin").write_bytes(b"x")
    with pytest.raises(CollectionError) as e:
        load_source_package(extra)
    assert e.value.code == "package_file_not_in_manifest"
    ok = build_collection_package(tmp_path / "CAT_v1.0.0")
    rows = (ok / "PACKAGE" / "SCENE_CATALOG.csv").read_text(encoding="utf-8").replace("9.45", "9.99")
    (ok / "PACKAGE" / "SCENE_CATALOG.csv").write_text(rows, encoding="utf-8")
    meta = json.loads((ok / "PACKAGE" / "MANIFEST.json").read_text(encoding="utf-8"))
    for f in meta["sha256_files"]:
        p = ok / f["path"]
        f["bytes"], f["sha256"] = p.stat().st_size, hashlib.sha256(p.read_bytes()).hexdigest()
    (ok / "PACKAGE" / "MANIFEST.json").write_text(json.dumps(meta), encoding="utf-8")
    built = build_records(load_source_package(ok), declaration_for(ok), tmp_path / "w3", ingest_id="x")
    assert any("package_catalog_mismatch:scene_cloud_pct" in i for i in built.issues)  # raporlanır, düzeltilmez


def test_subset_view_reads_original_files_without_copy(coll: Path, tmp_path: Path) -> None:
    pkg = load_source_package(coll)
    tar = next(e.path for e in pkg.entries if e.path.endswith(".tar"))
    sub = subset_package(pkg, [tar], tmp_path / "subset", "SUBSET_X")
    assert sub.path(tar) == pkg.path(tar) and not (tmp_path / "subset" / tar).exists()
    assert [p.name for p in (tmp_path / "subset").rglob("*") if p.is_file()] == ["MANIFEST.json"]
    built = build_records(sub, declaration_for(coll) | {"package_id": "SUBSET_X", "manifest_sha256": sub.manifest_sha256},
                          tmp_path / "w", ingest_id="x")
    assert built.item_count == 1 and any("item_count_differs" in i for i in built.issues)


# ----------------------------------------------------------------------------- doğrudan yayın

def _plan(coll: Path, data_root: Path, tmp_path: Path, **kw):
    return plan_collection(coll, declaration_for(coll), data_root=data_root, work_root=tmp_path / "work", provider="r2",
                           bucket=BUCKET, **kw)


def test_direct_publication_catalog_and_contract(coll: Path, data_root: Path, tmp_path: Path) -> None:
    from thermalis.cloud.catalog_sync import sync_catalog
    from thermalis.data import contract
    from thermalis.data.catalog_index import _query, query_completeness

    store, server = make_s3_backend(tmp_path)
    before = _tree_stat(coll)
    plan = _plan(coll, data_root, tmp_path)
    assert all(f.verified for f in plan.files if f.relpath != "PACKAGE/MANIFEST.json")
    result = publish_collection(store, plan, machine="m1")
    assert result["status"] == "PUBLISHED"
    commit = read_commit(store, release_prefix(DS, "1.0.0-rc1"))
    assert commit["access_class"] == "PUBLIC" and commit["verification"] == "full_sha256_read_back"
    assert verify_collection(store, plan)["status"] == "PASS"
    state = json.loads(store.open_read(layout.publish_state_key(DS, "1.0.0-rc1")).read())
    assert state["status"] == "ACCEPTED" and state["operation_id"] == plan.operation_id
    assert publish_collection(store, plan, machine="m1")["status"] == "ALREADY_PUBLISHED"
    assert _tree_stat(coll) == before
    # Yerel katalog: yalnız kayıtlar yansıtılır (veri indirilmez); sahneler ve bantlar dizinde.
    sync = sync_catalog(store, data_root, provider="r2", bucket=BUCKET)
    assert sync.status == "ok" and sync.mirrored == [f"{DS}@1.0.0-rc1"]
    assert not any(p.suffix == ".tar" for p in data_root.rglob("*"))
    col = contract.collection(data_root, LAYER)
    assert col["item_count"] == 2 and col["quality_status"] == "QA_CONDITIONAL" and col["access_class"] == "PUBLIC"
    assert [i["acquisition_date"] for i in col["items"]] == ["2020-06-28", "2022-06-29"]
    assert contract.collection(data_root, LAYER, platform="LANDSAT_9")["item_count"] == 1
    assert contract.collection(data_root, LAYER, max_cloud=10)["item_count"] == 1
    b10 = [b for b in col["band_summary"] if b["band_code"] == "ST_B10"]
    assert b10 and b10[0]["scale"] == float(SCALE) and b10[0]["add_offset"] == 149.0 and b10[0]["epsg"] == 32635
    item = contract.collection_item(data_root, LAYER, None, "LC09_L2SP_180031_20220629_20230409_02_T1")
    asset = next(a for a in item["assets"] if a["band_code"] == "ST_B10")
    assert asset["location"]["range_read"] == "vsisubfile_over_vsis3"
    assert asset["location"]["gdal_path"].startswith(f"/vsisubfile/{asset['offset']}_{asset['bytes']},/vsis3/{BUCKET}/releases/")
    desc = contract.descriptor(data_root, LAYER)
    assert desc["collection"]["item_count"] == 2 and desc["storage"]["backend"] == "r2"
    comp = {r["item"]: r["state"] for r in query_completeness(data_root, DS, "1.0.0-rc1")}
    assert comp["vector_fields"] == "not_applicable" and comp["temporal_coverage"] == "present"
    assert _query(data_root, "SELECT COUNT(*) AS n FROM collection_assets WHERE dataset_id=?", (DS,))[0]["n"] == 14


def test_source_change_after_indexing_blocks_publication(coll: Path, data_root: Path, tmp_path: Path) -> None:
    import os

    store, server = make_s3_backend(tmp_path)
    plan = _plan(coll, data_root, tmp_path)
    tar = next(f for f in plan.files if f.relpath.endswith(".tar"))
    st = tar.path.stat()
    os.utime(tar.path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))  # içerik aynı, zaman değişti
    with pytest.raises(DirectRefused) as e:
        publish_collection(store, plan, machine="m1")
    assert e.value.code == "source_changed_since_indexing" and not server.objects
    again = _plan(coll, data_root, tmp_path)  # yeniden plan: dosya yeniden doğrulanır, sabit kayıtlar aynı kalır
    assert again.records == plan.records
    assert publish_collection(store, again, machine="m1")["status"] == "PUBLISHED"


def test_direct_publication_refusals(coll: Path, data_root: Path, tmp_path: Path) -> None:
    restricted = plan_collection
    with pytest.raises(DirectRefused) as e:
        restricted(coll, declaration_for(coll, access_class="RESTRICTED"), data_root=data_root, work_root=tmp_path / "w",
                   provider="r2", bucket=BUCKET)
    assert e.value.code == "authorization_required"
    with pytest.raises(DirectRefused) as e:
        plan_collection(coll, {**declaration_for(coll), "manifest_sha256": "1" * 64}, data_root=data_root,
                        work_root=tmp_path / "w", provider="r2", bucket=BUCKET)
    assert e.value.code == "declaration_does_not_match_package"
    store, _ = make_s3_backend(tmp_path)
    plan = _plan(coll, data_root, tmp_path)
    claim = RenewableClaim(store, layout.publish_state_key(DS, "1.0.0-rc1"), operation_id="other-op", machine="m2",
                           schema=layout.PUBLISH_STATE_SCHEMA, ttl=timedelta(hours=12))
    claim.acquire()
    with pytest.raises(DirectRefused) as e:
        publish_collection(store, plan, machine="m1")
    assert e.value.code == "claimed_elsewhere"


def test_interrupted_publication_resumes_without_reuploading(coll: Path, data_root: Path, tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    plan = _plan(coll, data_root, tmp_path)
    cancel = CancelToken()

    def stop_after_first_object(stage: str, done: int, total: int) -> None:
        if stage == "upload" and done > 0:
            cancel.cancel()
    with pytest.raises(Cancelled):
        publish_collection(store, plan, machine="m1", cancel=cancel, progress=stop_after_first_object)
    assert read_commit(store, release_prefix(DS, "1.0.0-rc1")) is None  # yarım yayın görünmez
    first = {k: server.objects[k]["etag"] for k in server.objects if "/payload/" in k}
    assert first and len(first) < len(plan.files)
    payload_puts_before = server.calls["put_object"]
    assert publish_collection(store, plan, machine="m1")["status"] == "PUBLISHED"
    assert all(server.objects[k]["etag"] == etag for k, etag in first.items())  # zaten yazılmış nesneler yeniden yüklenmedi
    # İkinci çalıştırmada payload için yalnız eksik dosyalar yazıldı (+ kayıtlar, işaret, katalog girdisi, talep, işaretçi).
    payload_written = len([k for k in server.objects if "/payload/" in k]) - len(first)
    assert server.calls["put_object"] - payload_puts_before <= payload_written + len(plan.records) + 6


def test_verification_journal_skips_already_verified_objects(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    files = []
    for n in range(3):
        p = tmp_path / f"f{n}.bin"
        p.write_bytes(bytes([n]) * 1000)
        files.append(ReleaseFile(f"f{n}.bin", p, hashlib.sha256(p.read_bytes()).hexdigest(), 1000))
    records = {n: b"{}" for n in ("version.json", "quality.json", "integrity.json")}
    policy = PublicationPolicy(True, ("SYNTHETIC",))
    cancel = CancelToken()
    seen = {"n": 0}

    def stop_in_verify(stage: str, done: int, total: int) -> None:
        if stage == "verify":
            seen["n"] += 1
            if seen["n"] == 2:
                cancel.cancel()
    journal = tmp_path / "journal.json"
    kw = dict(dataset_id="THERMALIS_DATA/SYN03/X", version="1.0.0", access_class="SYNTHETIC", files=files, records=records,
              operation_id="op-journal-1", policy=policy, journal=journal)
    with pytest.raises(Cancelled):
        publish_release(store, cancel=cancel, progress=stop_in_verify, **kw)
    verified = json.loads(journal.read_text(encoding="utf-8"))["verified"]
    assert len(verified) >= 1
    gets_before = server.calls["get_object"]
    commit = publish_release(store, **kw)
    assert commit["operation_id"] == "op-journal-1"
    reread = server.calls["get_object"] - gets_before
    # Günlükteki nesneler yeniden okunmadı (+1: başlangıçtaki _COMMIT.json varlık denetimi).
    assert reread == len(files) + len(records) - len(verified) + 1


# ----------------------------------------------------------------------------- aralıklı erişim + önbellek

def test_member_materialization_downloads_only_member_range(coll: Path, data_root: Path, tmp_path: Path) -> None:
    from thermalis.cloud.access import materialize_member
    from thermalis.cloud.catalog_sync import sync_catalog
    from thermalis.objectstore.cache import ObjectCache

    store, server = make_s3_backend(tmp_path)
    plan = _plan(coll, data_root, tmp_path)
    publish_collection(store, plan, machine="m1")
    sync_catalog(store, data_root, provider="r2", bucket=BUCKET)
    reads: list[tuple[int, int]] = []
    original = store.read_range

    def counting(key, start, end):
        reads.append((start, end))
        return original(key, start, end)
    store.read_range = counting  # type: ignore[method-assign]
    full_gets = server.calls["get_object"]
    cache = ObjectCache(tmp_path / "cache", 64 * 1024 * 1024, protected_roots=[data_root])
    pid = "LC08_L2SP_180031_20200628_20200912_02_T1"
    m = materialize_member(data_root, store, cache, LAYER, None, pid, "ST_B10")
    col = json.loads((Path(contract_record_dir(data_root)) / "collection.json").read_text(encoding="utf-8"))
    asset = next(a for i in col["items"] if i["item_id"] == pid for a in i["assets"] if a.get("band_code") == "ST_B10")
    assert m["materialized"] and hashlib.sha256(Path(m["path"]).read_bytes()).hexdigest() == asset["sha256"]
    assert sum(e - s + 1 for s, e in reads) == asset["bytes"] and reads[0][0] == asset["offset"]
    assert server.calls["get_object"] - full_gets == len(reads)  # yalnız aralık istekleri; arşivin tamamı okunmadı
    archive = coll / next(i["archive"]["path"] for i in col["items"] if i["item_id"] == pid)
    assert read_window(m["path"], 1, 0, 0, 16, 16) == read_window(vsisubfile(asset, archive.as_posix()), 1, 0, 0, 16, 16)
    # Bozuk aralık önbelleğe girmez.
    server.objects[next(k for k in server.objects if k.endswith(archive.name))]["body"] = b"\0" * archive.stat().st_size
    cache2 = ObjectCache(tmp_path / "cache2", 64 * 1024 * 1024, protected_roots=[data_root])
    with pytest.raises(Exception) as e:
        materialize_member(data_root, store, cache2, LAYER, None, pid, "ST_B10")
    assert "does_not_match" in str(e.value)


def contract_record_dir(data_root: Path) -> str:
    from thermalis.data.catalog_index import record_folder

    return str(record_folder(data_root, DS, "1.0.0-rc1"))


# ----------------------------------------------------------------------------- talepler ve kilitler

def test_renewable_claim_takeover_and_loss(tmp_path: Path) -> None:
    store, _ = make_s3_backend(tmp_path)
    clock = {"t": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    now = lambda: clock["t"]  # noqa: E731
    key = "state/publish/X/1.0.0.json"
    a = RenewableClaim(store, key, operation_id="op-a", machine="A", schema="s", ttl=timedelta(hours=1), now=now)
    b = RenewableClaim(store, key, operation_id="op-b", machine="B", schema="s", ttl=timedelta(hours=1), now=now)
    assert a.acquire() == ("claimed", None)
    clock["t"] += timedelta(minutes=50)
    a.renew()  # kalp atışı: süre son yenilemeden ölçülür
    clock["t"] += timedelta(minutes=50)
    with pytest.raises(Busy):
        b.acquire()  # 100 dk geçti ama son kalp atışı 50 dk önce
    clock["t"] += timedelta(minutes=61)
    assert b.acquire() == ("claimed", None)  # kalp atışı kesildi: devralındı
    with pytest.raises(ClaimLost):
        a.renew(force=True)
    b.finish("ACCEPTED")
    c = RenewableClaim(store, key, operation_id="op-c", machine="C", schema="s", ttl=timedelta(hours=1), now=now)
    state, terminal = c.acquire()
    assert state == "terminal" and terminal["status"] == "ACCEPTED"
    # Aynı işlem kimliği (yeniden başlatma) kendi talebini sürdürür.
    d = RenewableClaim(store, "state/publish/Y/1.json", operation_id="op-d", machine="D", schema="s", ttl=timedelta(hours=1), now=now)
    d.acquire()
    d2 = RenewableClaim(store, "state/publish/Y/1.json", operation_id="op-d", machine="D", schema="s", ttl=timedelta(hours=1), now=now)
    assert d2.acquire() == ("claimed", None)


def test_local_lock_heartbeat_and_stale_recovery(tmp_path: Path) -> None:
    import os

    lock = LocalLock(tmp_path / "x.lock", stale_seconds=60, touch_every=0)
    assert lock.acquire()
    other = LocalLock(tmp_path / "x.lock", stale_seconds=60)
    assert not other.acquire()
    old = time.time() - 120
    os.utime(tmp_path / "x.lock", (old, old))
    assert other.acquire()  # kalp atışı kesilmiş kilit kaldırıldı
    other.release()
    assert not (tmp_path / "x.lock").exists()


# ----------------------------------------------------------------------------- bulut kabulü ön denetimi

def _announce(store, package_id: str, files: list[tuple[str, int]]) -> None:
    marker = {"schema": layout.UPLOAD_SCHEMA, "package_id": package_id, "manifest_sha256": "a" * 64, "dataset_id": DS,
              "version": "1.0.0", "access_class": "PUBLIC", "total_bytes": sum(s for _, s in files),
              "files": [{"path": p, "bytes": s, "sha256": "b" * 64} for p, s in files]}
    store.put_bytes(layout.ready_key(package_id), json.dumps(marker).encode(), create_only=True, operation_id="t")
    store.put_bytes(layout.announcement_key(package_id), b"{}", create_only=True, operation_id="t")


def test_cloud_intake_preflight_defers_without_claim_or_download(tmp_path: Path, data_root: Path) -> None:
    from thermalis.cloud.intake import CloudIntake
    from thermalis.data.ingest import load_intake

    store, server = make_s3_backend(tmp_path)
    _announce(store, "HD99_TAR_v1", [("PACKAGE/MANIFEST.json", 10), ("SOURCE/2020/a.tar", 62 * 1024 ** 3)])
    _announce(store, "HD98_NC_v1", [("PACKAGE/MANIFEST.json", 10), ("x.nc", 5)])
    intake = CloudIntake(store, store, data_root, provider="r2", bucket=BUCKET, intake_bucket=BUCKET, namespace="",
                         staging_root=tmp_path / "staging", staging_max_bytes=10 ** 9, disk_free=lambda p: 10 ** 9, max_packages=1)
    result = intake.scan()
    outcomes = {o.package_id: (o.status, o.code) for o in result.outcomes}
    assert outcomes["HD99_TAR_v1"] == ("DEFERRED", "raster_collection_direct_publication")
    assert outcomes["HD98_NC_v1"][0] == "DEFERRED" and outcomes["HD98_NC_v1"][1] == "adapter_required"  # max_packages tükenmedi
    assert not any(k.startswith("state/intake/") for k in server.objects)  # talep yok
    assert not any(p.is_file() and p.name != "cloud-intake.lock" for p in (tmp_path / "staging").rglob("*"))  # indirme yok
    packages = load_intake(data_root)["packages"]
    states = {e["name"]: (e["state"], e["reason_code"]) for e in packages.values()}
    assert states["HD99_TAR_v1"] == ("ADAPTER_REQUIRED", "raster_collection_direct_publication")
    assert states["HD98_NC_v1"] == ("ADAPTER_REQUIRED", "adapter_required")
    assert not any(e.get("cloud_state") == "REJECTED" for e in packages.values())  # kalıcı RED yok


def test_local_watcher_reports_declared_collection(coll: Path, data_root: Path) -> None:
    from thermalis.data.ingest import assess

    c = assess(coll, data_root, settle_seconds=0)
    assert c.state == "DIRECT_PUBLICATION" and "declaration_missing" in c.reason
    decl.save(data_root, declaration_for(coll))
    c = assess(coll, data_root, settle_seconds=0)
    assert c.state == "DIRECT_PUBLICATION" and "declaration_ok" in c.reason and DS in c.reason


def test_watcher_cloud_scan_does_not_block_local_scan(tmp_path: Path, monkeypatch) -> None:
    from thermalis.data.intake import IntakeWatcher
    from thermalis.settings import Settings

    release = threading.Event()
    entered = threading.Event()

    class SlowCloud:
        def scan(self):
            entered.set()
            release.wait(10)
            from thermalis.cloud.intake import CloudScanResult

            return CloudScanResult("ok")

    incoming = tmp_path / "incoming"
    incoming.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    settings = Settings(data_root=str(root), transfer_watch_enabled=True, transfer_incoming_dir=str(incoming),
                        transfer_scan_interval_seconds=10)
    settings.cloud.endpoint, settings.cloud.bucket = "https://example.invalid", BUCKET
    settings.cloud_intake.enabled = True
    watcher = IntakeWatcher(settings_loader=lambda: settings, cloud_factory=lambda s, r: SlowCloud())
    watcher.start()
    try:
        assert entered.wait(10)  # bulut taraması uzun bir işte bekliyor
        scans = watcher.status.scans
        watcher.trigger()
        deadline = time.time() + 10
        while watcher.status.scans == scans and time.time() < deadline:
            time.sleep(0.05)
        assert watcher.status.scans > scans  # yerel tarama bloklanmadı
    finally:
        release.set()
        watcher.stop()
