"""Testler için sentetik raster koleksiyonu: Landsat C2 L2 düzeninde küçük TAR arşivleri (gerçek veri değil).

Her sahne arşivi: ``*_MTL.json`` (kaynak beyanlı ölçek/kaydırma), ``*_ST_stac.json``, küçük COG'lar (ST_B10, SR_B4,
QA_PIXEL) ve bir küçük görsel. Değerler deterministiktir; hiçbir gerçek ölçüm temsil edilmez.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import tarfile
from pathlib import Path

from osgeo import gdal, osr

gdal.UseExceptions()

SCALE, ADD = "0.00341802", "149.0"
SR_SCALE, SR_ADD = "2.75e-05", "-0.2"
STATUS = "SOURCE_QA_PASS_WITH_DOCUMENTED_SPATIAL_LIMITATIONS"


def cog_bytes(*, width: int = 256, height: int = 192, dtype=gdal.GDT_UInt16, nodata: float | None = 0, epsg: int = 32635,
              seed: int = 1) -> bytes:
    mem = gdal.GetDriverByName("MEM").Create("", width, height, 1, dtype)
    mem.SetGeoTransform([560985.0, 30.0, 0.0, 4741215.0, 0.0, -30.0])
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    mem.SetProjection(srs.ExportToWkt())
    band = mem.GetRasterBand(1)
    if nodata is not None:
        band.SetNoDataValue(nodata)
    import struct

    fmt = {gdal.GDT_UInt16: "H", gdal.GDT_Int16: "h", gdal.GDT_Byte: "B"}[dtype]
    values = [((x * 7 + y * 13 + seed * 101) % 60000) + 1 for y in range(height) for x in range(width)]
    if fmt == "B":
        values = [v % 255 for v in values]
    if fmt == "h":
        values = [v % 30000 for v in values]
    values[0] = int(nodata) if nodata is not None else values[0]  # bir dolgu pikseli
    band.WriteRaster(0, 0, width, height, struct.pack(f"<{width * height}{fmt}", *values))
    path = f"/vsimem/fake_{seed}_{width}_{height}_{dtype}.tif"
    gdal.Translate(path, mem, format="COG", creationOptions=["COMPRESS=DEFLATE", "BLOCKSIZE=128", "OVERVIEWS=IGNORE_EXISTING"])
    f = gdal.VSIFOpenL(path, "rb")
    gdal.VSIFSeekL(f, 0, 2)
    size = gdal.VSIFTellL(f)
    gdal.VSIFSeekL(f, 0, 0)
    data = gdal.VSIFReadL(1, size, f)
    gdal.VSIFCloseL(f)
    gdal.Unlink(path)
    return bytes(data)


def scene_members(product_id: str, date: str, platform: str, cloud: float, seed: int) -> dict[str, bytes]:
    mtl = {"LANDSAT_METADATA_FILE": {
        "PRODUCT_CONTENTS": {"LANDSAT_PRODUCT_ID": product_id, "PROCESSING_LEVEL": "L2SP", "COLLECTION_NUMBER": "02",
                             "COLLECTION_CATEGORY": "T1", "DIGITAL_OBJECT_IDENTIFIER": "https://doi.org/10.5066/P9OGBGM6",
                             "FILE_NAME_BAND_ST_B10": f"{product_id}_ST_B10.TIF", "FILE_NAME_BAND_4": f"{product_id}_SR_B4.TIF"},
        "IMAGE_ATTRIBUTES": {"SPACECRAFT_ID": platform, "SENSOR_ID": "OLI_TIRS", "WRS_TYPE": "2", "WRS_PATH": "180", "WRS_ROW": "31",
                             "DATE_ACQUIRED": date, "SCENE_CENTER_TIME": "08:47:21.0000000Z", "CLOUD_COVER": f"{cloud:.2f}",
                             "CLOUD_COVER_LAND": f"{cloud + 1:.2f}"},
        "PROJECTION_ATTRIBUTES": {"MAP_PROJECTION": "UTM", "DATUM": "WGS84", "UTM_ZONE": "35", "GRID_CELL_SIZE_THERMAL": "30.00",
                                  "CORNER_UL_LAT_PRODUCT": "42.8", "CORNER_UL_LON_PRODUCT": "27.7", "CORNER_UR_LAT_PRODUCT": "42.7",
                                  "CORNER_UR_LON_PRODUCT": "30.5", "CORNER_LL_LAT_PRODUCT": "40.7", "CORNER_LL_LON_PRODUCT": "27.7",
                                  "CORNER_LR_LAT_PRODUCT": "40.6", "CORNER_LR_LON_PRODUCT": "30.4"},
        "LEVEL2_PROCESSING_RECORD": {"DATE_PRODUCT_GENERATED": "2020-09-12T17:52:29Z", "PROCESSING_SOFTWARE_VERSION": "FAKE"},
        "LEVEL2_SURFACE_REFLECTANCE_PARAMETERS": {"REFLECTANCE_MULT_BAND_4": SR_SCALE, "REFLECTANCE_ADD_BAND_4": SR_ADD,
                                                  "QUANTIZE_CAL_MIN_BAND_4": "1", "QUANTIZE_CAL_MAX_BAND_4": "65535"},
        "LEVEL2_SURFACE_TEMPERATURE_PARAMETERS": {"TEMPERATURE_MULT_BAND_ST_B10": SCALE, "TEMPERATURE_ADD_BAND_ST_B10": ADD,
                                                  "QUANTIZE_CAL_MINIMUM_BAND_ST_B10": "1", "QUANTIZE_CAL_MAXIMUM_BAND_ST_B10": "65535"},
        "LEVEL1_PROCESSING_RECORD": {"LANDSAT_SCENE_ID": f"FAKE{seed:04d}"}}}
    stac = {"type": "Feature", "bbox": [27.7, 40.6, 30.5, 42.8], "properties": {"datetime": f"{date}T08:47:21Z", "proj:epsg": 32635,
                                                                             "instruments": ["OLI", "TIRS"]},
            "assets": {"lwir11": {"href": f"https://example.invalid/{product_id}_ST_B10.TIF", "title": "Surface Temperature Band (B10)",
                                  "roles": ["data", "temperature"], "eo:bands": [{"name": "B10", "common_name": "lwir11"}]},
                       "qa_pixel": {"href": f"https://example.invalid/{product_id}_QA_PIXEL.TIF", "title": "Pixel QA",
                                    "roles": ["cloud"], "classification:bitfields": [{"name": "fill", "offset": 0, "length": 1}]}}}
    return {
        f"{product_id}_MTL.json": json.dumps(mtl).encode("utf-8"),
        f"{product_id}_ST_stac.json": json.dumps(stac).encode("utf-8"),
        f"{product_id}_QA_PIXEL.TIF": cog_bytes(nodata=None, seed=seed + 2),
        f"{product_id}_SR_B4.TIF": cog_bytes(seed=seed + 1),
        f"{product_id}_ST_B10.TIF": cog_bytes(seed=seed),
        f"{product_id}_ST_QA.TIF": cog_bytes(dtype=gdal.GDT_Int16, nodata=-9999, seed=seed + 3),
        f"{product_id}_thumb_small.jpeg": b"\xff\xd8fake-thumbnail" + bytes([seed % 256]) * 300,
    }


def write_tar(path: Path, members: dict[str, bytes], *, fmt=tarfile.USTAR_FORMAT) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w", format=fmt) as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 1_600_000_000, 0o644
            tar.addfile(info, io.BytesIO(data))


SCENES = [("LC08_L2SP_180031_20200628_20200912_02_T1", "2020-06-28", "LANDSAT_8", 9.45),
          ("LC09_L2SP_180031_20220629_20230409_02_T1", "2022-06-29", "LANDSAT_9", 26.16)]


def build_collection_package(root: Path, *, scenes=SCENES, browser_suffix: bool = True, extra_files: dict[str, bytes] | None = None,
                             manifest_override=None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, (pid, date, platform, cloud) in enumerate(scenes):
        name = f"{pid} (3).tar" if browser_suffix and i == 0 else f"{pid}.tar"  # tarayıcı son eki: yeniden adlandırılmaz
        rel = f"SOURCE/{date[:4]}/{name}"
        write_tar(root / rel, scene_members(pid, date, platform, cloud, seed=i + 1))
        data = (root / rel).read_bytes()
        rows.append({"year": date[:4], "product_id": pid, "relative_path": rel, "bytes": str(len(data)),
                     "sha256": hashlib.sha256(data).hexdigest(), "satellite": platform, "acquisition_date": date,
                     "scene_cloud_pct": f"{cloud:.2f}", "source_qa_status": "QA_COMPLETE"})
    pkg = root / "PACKAGE"
    pkg.mkdir(exist_ok=True)
    (pkg / "RELEASE_CONTRACT.json").write_text(json.dumps({
        "source_data_status": STATUS, "documented_limitations": ["seasonal sampling", "not continuous"],
        "source_scene_count": len(scenes), "preserve_raw": True}), encoding="utf-8")
    (pkg / "SCHEMA_AND_UNITS.json").write_text(json.dumps({"scientific_status": STATUS}), encoding="utf-8")
    (pkg / "QA_SCIENTIFIC_LIMITATIONS.md").write_text("# Sentetik sınırlamalar\n- örnek\n", encoding="utf-8")
    with (pkg / "SCENE_CATALOG.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    for rel, data in (extra_files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    files = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.name != "MANIFEST.json")
    meta = {"package": root.name, "family": "THERMALIS_HEAT_SYN_LST", "science_qa_status": STATUS, "file_count": len(files),
            "readiness_status": "READY_FOR_STAGING_CHECKS_ONLY", "created_utc": "2026-11-12T12:42:25+00:00",
            "sha256_files": [{"path": f, "bytes": (root / f).stat().st_size,
                              "sha256": hashlib.sha256((root / f).read_bytes()).hexdigest()} for f in files]}
    if manifest_override:
        manifest_override(meta)
    (pkg / "MANIFEST.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return root


def declaration_spec(**override) -> dict:
    spec = {
        "dataset_id": "THERMALIS_DATA/SYN02_LST/TESTKENT_180031", "version": "1.0.0-rc1", "access_class": "PUBLIC",
        "access_basis": "Sentetik test: kaynak ürünler PUBLIC beyan edildi.",
        "source": {"organization": "SYNTHETIC", "product": "Synthetic L2 scenes", "citation": "Synthetic citation (test).",
                   "doi": "https://doi.org/10.0000/synthetic"},
        "supplementary_records": {"license": "not_declared", "basis": "test"},
        "quality": {"source_status_ref": "PACKAGE/RELEASE_CONTRACT.json#source_data_status",
                    "cross_check_refs": ["PACKAGE/MANIFEST.json#science_qa_status", "PACKAGE/SCHEMA_AND_UNITS.json#scientific_status"],
                    "documented_limitations_ref": "PACKAGE/RELEASE_CONTRACT.json#documented_limitations",
                    "limitations_documents": ["PACKAGE/QA_SCIENTIFIC_LIMITATIONS.md"]},
        "collection": {"interpreter": "landsat_c2_l2", "layer_name": "SCENES",
                       "expected_item_count_ref": "PACKAGE/RELEASE_CONTRACT.json#source_scene_count",
                       "scene_catalog": {"path": "PACKAGE/SCENE_CATALOG.csv", "archive_column": "relative_path",
                                         "checks": {"product_id": "properties.product_id", "acquisition_date": "properties.acquisition_date",
                                                    "sha256": "archive.sha256", "bytes": "archive.bytes", "scene_cloud_pct": "properties.cloud_cover"}}},
        "declared_by": "test",
    }
    spec.update(override)
    return spec


def declaration_for(root: Path, **override) -> dict:
    from thermalis.data import declaration as decl
    from thermalis.data.collection import load_source_package

    pkg = load_source_package(root)
    return decl.validate({**declaration_spec(**override), "schema": decl.SCHEMA, "package_id": pkg.package_id,
                          "manifest_sha256": pkg.manifest_sha256, "declared_at": "2026-11-26T00:00:00Z"})
