"""ZIP aktarım paketleri: üye dizini, bütünlük, keşif (GDB/raster/tablo), beyan, doğrudan yayın, katalog ve
arşiv içi üyelerin doğrulanmış malzemeleştirilmesi (sentetik; ağ yok)."""

from __future__ import annotations

import hashlib
import json
import zipfile
import zlib
from pathlib import Path

import pytest

from bundlefake import CSV, build_bundle, bundle_declaration
from s3fake import make_s3_backend
from thermalis.cloud.direct import plan_collection, publish_collection, verify_collection
from thermalis.data import declaration as decl
from thermalis.data.bundle import build_bundle_records, is_bundle, load_bundle
from thermalis.data.collection import CollectionError
from thermalis.data.formats.preflight import preflight
from thermalis.data.formats.tabular import profile_bytes
from thermalis.data.formats.zip_index import index_zip, vsizip

BUCKET = "thermalis-test-bucket"
DS = "THERMALIS_DATA/SYN07_BUNDLE/TESTKENT"


def layers_local(name: str, path: str) -> int:
    from osgeo import gdal

    ds = gdal.OpenEx(path, gdal.OF_VECTOR)
    try:
        return ds.GetLayerByName(name).GetFeatureCount()
    finally:
        ds = None


def _snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(root.iterdir())}


@pytest.fixture()
def bundle(tmp_path: Path) -> Path:
    return build_bundle(tmp_path / "SYN07_BUNDLE_v1.0.0-rc1")


def test_zip_index_offsets_allow_member_range_reads(bundle: Path) -> None:
    archive = next(bundle.glob("*.zip"))
    idx = index_zip(archive).by_name()
    raw = archive.read_bytes()
    with zipfile.ZipFile(archive) as zf:
        for name in ("SOURCE_ORIGINAL/SOURCE/SERIES.csv", "SOURCE_ORIGINAL/ORIGINAL_DOWNLOADS/tile.DEM.zip"):
            m = idx[name]
            chunk = raw[m.data_offset:m.data_offset + m.compressed_size]
            data = zlib.decompressobj(-15).decompress(chunk) if m.method == "deflate" else chunk
            assert data == zf.read(name) and hashlib.sha256(data).hexdigest() == m.sha256
    assert idx["SOURCE_ORIGINAL/ORIGINAL_DOWNLOADS/tile.DEM.zip"].method == "stored"


def test_tabular_profile_sections_units_and_time_without_conversion() -> None:
    p = profile_bytes(CSV.encode("utf-8"))
    assert p["delimiter"] == "," and [s["records"] for s in p["sections"]] == [1, 24]
    cols = {c["name"]: c for c in p["sections"][1]["columns"]}
    assert cols["temperature_2m (°C)"]["header_declared_unit"] == "°C"
    assert cols["shortwave_radiation (W/m²)"]["header_declared_unit"] == "W/m²"
    assert cols["label (Aile, akraba vb.)"]["header_declared_unit"] is None  # etiket birim sayılmaz
    t = cols["time"]["temporal"]
    assert (t["first_label"], t["last_label"], t["distinct_labels"], t["repeated_labels"]) == ("2020-06-01T00:00", "2020-06-01T23:00", 24, 0)
    assert cols["temperature_2m (°C)"]["numeric_min"] == 20.0  # değer olduğu gibi (birim çevrilmez)
    semi = profile_bytes("﻿A;B\n1;x\n;y\n".encode("utf-8"))
    assert semi["bom"] and semi["delimiter"] == ";" and semi["sections"][0]["columns"][0]["empty_count"] == 1


def test_preflight_and_bundle_detection(bundle: Path) -> None:
    names = [p.name for p in bundle.iterdir()]
    assert preflight(names).route == "zip_bundle" and is_bundle(bundle)
    assert preflight(["loose.zip"]).route is None  # yan dosyasız ZIP hâlâ bağdaştırıcı gerektirir


def test_bundle_records_discover_layers_rasters_tables(bundle: Path, tmp_path: Path) -> None:
    before = _snapshot(bundle)
    pkg = load_bundle(bundle)
    built = build_bundle_records(pkg, bundle_declaration(bundle), tmp_path / "work", ingest_id="t")
    assert _snapshot(bundle) == before  # kaynak paket aynı; arşiv çıkarılmadı
    v = json.loads(built.records["version.json"])
    q = json.loads(built.records["quality.json"])
    b = json.loads(built.records["bundle.json"])
    layers = v["layers"]
    assert {"DISTRICTS", "NEIGHBORHOODS", "SURFACE_30M", "SERIES"} <= set(layers)
    assert layers["DISTRICTS"]["geometry_type"] == "Multi Polygon" and layers["DISTRICTS"]["role"] == "reference_layer"
    assert layers["NEIGHBORHOODS"]["feature_count"] == 3 and any(f["name"] == "ILCEKN" for f in layers["NEIGHBORHOODS"]["fields"])
    r = layers["SURFACE_30M"]
    assert r["geometry_type"] == "Raster" and r["raster"]["dtype"] == "UInt16" and r["raster"]["epsg"] == 32635
    assert r["raster"]["vertical_crs"] is None and r["role"] == "prepared_dsm_primary"  # dikey referans beyan yoksa uydurulmaz
    t = layers["SERIES"]
    assert t["geometry_type"] == "None" and t["feature_count"] == 24 and t["table"]["sections"] == 2
    assert {f["name"]: f["unit"] for f in t["fields"] if f["unit"]} == {"temperature_2m (°C)": "°C", "wind_speed_10m (km/h)": "km/h",
                                                                          "shortwave_radiation (W/m²)": "W/m²"}
    tc = v["source"]["temporal_coverage"]
    assert (tc["semantics"], tc["start"], tc["end"]) == ("source_acquisition_span", "2011-03-10T16:05:11Z", "2014-08-26T16:06:41Z")
    assert q["status"] == "QA_CONDITIONAL" and q["source_status"] == "QA_CONDITIONAL" and q["item_issues"] == []
    assert len(b["nested_archives"]) == 1 and b["nested_archives"][0]["member"].endswith("tile.DEM.zip")
    assert "package/RELEASE_STATUS.json" in built.records and "package/LICENSE_AND_ACCESS.md" in built.records
    integ = json.loads(built.records["integrity.json"])
    origin = {f["path"]: f["checksum_origin"] for f in integ["files"]}
    assert origin["SYN07_BUNDLE_v1.0.0-rc1.zip"] == "sidecar_sha256" and origin["TECHNICAL_DOCUMENTATION.md"] == "computed_at_registration"


def test_bundle_integrity_failures_are_detected(tmp_path: Path) -> None:
    bad = build_bundle(tmp_path / "A" / "SYN07_BUNDLE_v1.0.0-rc1", bad_sidecar=True)
    with pytest.raises(CollectionError) as e:
        build_bundle_records(load_bundle(bad), bundle_declaration(bad), tmp_path / "w1", ingest_id="t")
    assert e.value.code == "sha256_mismatch"
    bad = build_bundle(tmp_path / "B" / "SYN07_BUNDLE_v1.0.0-rc1", corrupt_member=True)
    with pytest.raises(CollectionError) as e:
        build_bundle_records(load_bundle(bad), bundle_declaration(bad), tmp_path / "w2", ingest_id="t")
    assert e.value.code == "inner_member_sha256_mismatch"
    bad = build_bundle(tmp_path / "C" / "SYN07_BUNDLE_v1.0.0-rc1", unlisted=True)
    with pytest.raises(CollectionError) as e:
        build_bundle_records(load_bundle(bad), bundle_declaration(bad), tmp_path / "w3", ingest_id="t")
    assert e.value.code == "member_not_in_inner_manifest"


def test_bundle_declaration_rules(bundle: Path) -> None:
    d = bundle_declaration(bundle)
    with pytest.raises(decl.DeclarationError) as e:
        decl.validate({**d, "collection": {"layer_name": "X"}})
    assert e.value.code == "declaration_kind_ambiguous"
    with pytest.raises(decl.DeclarationError) as e:
        decl.validate({**d, "temporal": {"semantics": "passport_validity"}})
    assert e.value.code == "declaration_temporal_semantics_invalid"
    with pytest.raises(decl.DeclarationError):
        decl.validate({**d, "manifest_sha256": "f" * 64}, manifest_sha256=d["manifest_sha256"])


def test_bundle_journal_and_pinned_records_avoid_rework(bundle: Path, tmp_path: Path, monkeypatch) -> None:
    import thermalis.data.bundle as mod

    pkg = load_bundle(bundle)
    first = build_bundle_records(pkg, bundle_declaration(bundle), tmp_path / "w", ingest_id="t")

    def boom(*a, **k):
        raise AssertionError("re-indexed")
    monkeypatch.setattr(mod, "index_zip", boom)
    again = build_bundle_records(pkg, bundle_declaration(bundle), tmp_path / "w", ingest_id="t")
    assert again.reused and again.records == first.records


def test_bundle_direct_publication_catalog_and_member_access(bundle: Path, data_root: Path, tmp_path: Path) -> None:
    from osgeo import gdal, ogr

    from thermalis.cloud.access import materialize_asset
    from thermalis.cloud.catalog_sync import sync_catalog
    from thermalis.data import contract
    from thermalis.data.vector_inspect import layer_facts
    from thermalis.objectstore.cache import ObjectCache

    store, server = make_s3_backend(tmp_path)
    before = _snapshot(bundle)
    plan = plan_collection(bundle, bundle_declaration(bundle), data_root=data_root, work_root=tmp_path / "work", provider="r2",
                           bucket=BUCKET)
    assert {f.relpath for f in plan.files} == set(before)  # arşiv ÇIKARILMADAN, teslim klasörü bayt bayt
    assert publish_collection(store, plan, machine="m1")["status"] == "PUBLISHED"
    assert verify_collection(store, plan, full=False)["status"] == "PASS"
    assert _snapshot(bundle) == before
    sync = sync_catalog(store, data_root, provider="r2", bucket=BUCKET)
    assert sync.status == "ok" and sync.mirrored
    lid = f"{DS}/NEIGHBORHOODS"
    desc = contract.descriptor(data_root, lid)
    assert desc["asset"]["format"] == "filegdb_vector" and desc["storage"]["backend"] == "r2"
    assert desc["storage"]["gdal_path"].startswith(f"/vsizip/{{/vsis3/{BUCKET}/releases/SYN07_BUNDLE/TESTKENT/1.0.0-rc1/payload/")
    assert desc["storage"]["archive_member"] == "SOURCE_ORIGINAL/Synthetic.gdb"
    assert desc["quality"]["status"] == "QA_CONDITIONAL"
    sci = contract.descriptor(data_root, f"{DS}/SERIES")["scientific"]
    assert sci["record_kind"] == "source_bundle" and sci["field_units"]["temperature_2m (°C)"] == "°C"
    assert sci["temporal_coverage"]["semantics"] == "source_acquisition_span"
    raster = contract.descriptor(data_root, f"{DS}/SURFACE_30M")["scientific"]["raster"]
    assert raster["width"] and raster["resolution"] == [30.0, 30.0]
    # Arşiv içindeki GDB klasörü: yalnız ilgili üyelerin sıkıştırılmış aralıkları okunur, yerelde açılır, SHA-256 doğrulanır.
    reads = []
    original = store.read_range

    def counting(key, start, end):
        reads.append(end - start + 1)
        return original(key, start, end)
    store.read_range = counting  # type: ignore[method-assign]
    cache = ObjectCache(tmp_path / "cache", 64 * 1024 * 1024, protected_roots=[data_root])
    m = materialize_asset(data_root, store, cache, lid)
    assert m["materialized"] and Path(m["path"]).name == "Synthetic.gdb"
    archive = next(bundle.glob("*.zip"))
    local = vsizip(archive.as_posix(), "SOURCE_ORIGINAL/Synthetic.gdb")
    assert layer_facts(Path(m["path"]), "Neighborhoods")["content_id"] == layer_facts(local, "Neighborhoods")["content_id"]
    assert sum(reads) < archive.stat().st_size  # arşivin tamamı indirilmedi
    gdal.UseExceptions()
    ds = gdal.OpenEx(str(m["path"]), gdal.OF_VECTOR)
    assert ds.GetLayerByName("Districts").GetFeatureCount() == layers_local("Districts", local)
    ds = None
    r = materialize_asset(data_root, store, cache, f"{DS}/SURFACE_30M")
    with zipfile.ZipFile(archive) as zf:
        assert Path(r["path"]).read_bytes() == zf.read("PREPARED_INPUT/SURFACE_30M.tif")
    del ogr


def test_cloud_intake_defers_bundle_and_watcher_reports_it(bundle: Path, tmp_path: Path, data_root: Path) -> None:
    from thermalis.cloud import layout
    from thermalis.cloud.intake import CloudIntake
    from thermalis.data.ingest import assess

    store, server = make_s3_backend(tmp_path)
    files = [{"path": p.name, "bytes": p.stat().st_size, "sha256": "b" * 64} for p in sorted(bundle.iterdir())]
    marker = {"schema": layout.UPLOAD_SCHEMA, "package_id": "SYN07_X", "manifest_sha256": "a" * 64, "dataset_id": DS,
              "version": "1.0.0", "access_class": "PUBLIC", "total_bytes": sum(f["bytes"] for f in files),
              "files": files + [{"path": "PACKAGE/MANIFEST.json", "bytes": 1, "sha256": "c" * 64}]}
    marker["total_bytes"] += 1
    store.put_bytes(layout.ready_key("SYN07_X"), json.dumps(marker).encode(), create_only=True, operation_id="t")
    store.put_bytes(layout.announcement_key("SYN07_X"), b"{}", create_only=True, operation_id="t")
    intake = CloudIntake(store, store, data_root, provider="r2", bucket=BUCKET, intake_bucket=BUCKET, namespace="",
                         staging_root=tmp_path / "staging", staging_max_bytes=10 ** 9, disk_free=lambda p: 10 ** 9)
    del intake  # PACKAGE/MANIFEST.json içeren işaret bundle sayılmaz; bundle dizisi yerel izleyicide doğrulanır
    c = assess(bundle, data_root, settle_seconds=0)
    assert c.state == "DIRECT_PUBLICATION" and "source_bundle; declaration_missing" in c.reason
    decl.save(data_root, bundle_declaration(bundle))
    c = assess(bundle, data_root, settle_seconds=0)
    assert c.state == "DIRECT_PUBLICATION" and "declaration_ok" in c.reason
    pf = preflight([f["path"] for f in files])
    assert pf.route == "zip_bundle"
