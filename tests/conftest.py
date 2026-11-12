"""Test düzeneği: gerçek veriye dokunmadan HD01 yapısında sentetik aktarım paketi üretir."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

ESRI_WKT = (
    'PROJCS["ITRF_96_UTM_Zone_35N",GEOGCS["GCS_GRS_1980",DATUM["D_ITRF_1996",SPHEROID["GRS_1980",6378137.0,298.257222101]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],PROJECTION["Transverse_Mercator"],'
    'PARAMETER["False_Easting",500000.0],PARAMETER["False_Northing",0.0],PARAMETER["Central_Meridian",30.0],'
    'PARAMETER["Scale_Factor",1.0],PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]'
    ";-5123200 -10002100 10000;-100000 10000;-100000 10000;0.001;0.001;0.001;IsHighPrecision"
)
ILCE_UAVT = {"1103": "ADALAR", "1421": "KADIKÖY"}
YAKA = {"1": "ANADOLU", "2": "AVRUPA"}


def _square(x: float, y: float, size: float = 100.0):
    from osgeo import ogr

    return ogr.CreateGeometryFromWkt(f"POLYGON(({x} {y},{x + size} {y},{x + size} {y + size},{x} {y + size},{x} {y}))")


def _build_gdb(path: Path) -> None:
    from osgeo import gdal, ogr, osr

    gdal.UseExceptions()
    ds = gdal.GetDriverByName("OpenFileGDB").Create(str(path), 0, 0, 0, gdal.GDT_Unknown)
    ds.AddFieldDomain(ogr.CreateCodedFieldDomain("Ilce_UAVT", "UAVT İlçe Kodları", ogr.OFTInteger, ogr.OFSTNone, {int(k): v for k, v in ILCE_UAVT.items()}))
    ds.AddFieldDomain(ogr.CreateCodedFieldDomain("YAKA", "Yaka", ogr.OFTInteger, ogr.OFSTInt16, {int(k): v for k, v in YAKA.items()}))
    srs = osr.SpatialReference()
    srs.ImportFromWkt(ESRI_WKT.split(";")[0])
    opts = ["CREATE_SHAPE_AREA_AND_LENGTH_FIELDS=YES"]

    districts = ds.CreateLayer("Districts", srs, ogr.wkbPolygon, opts)
    for name, kind in (("ILKN", ogr.OFTInteger), ("KIMLIKNO", ogr.OFTInteger)):
        districts.CreateField(ogr.FieldDefn(name, kind))
    ad = ogr.FieldDefn("AD", ogr.OFTString)
    ad.SetWidth(50)
    districts.CreateField(ad)
    for i, (code, label) in enumerate(ILCE_UAVT.items()):
        f = ogr.Feature(districts.GetLayerDefn())
        f.SetField("ILKN", 34)
        f.SetField("KIMLIKNO", int(code))
        f.SetField("AD", label)
        f.SetGeometry(_square(500000 + i * 200, 4500000, 200))
        districts.CreateFeature(f)

    hoods = ds.CreateLayer("Neighborhoods", srs, ogr.wkbPolygon, opts)
    ilcekn = ogr.FieldDefn("ILCEKN", ogr.OFTInteger)
    ilcekn.SetDomainName("Ilce_UAVT")
    hoods.CreateField(ilcekn)
    yaka = ogr.FieldDefn("YAKA", ogr.OFTInteger)
    yaka.SetSubType(ogr.OFSTInt16)
    yaka.SetDomainName("YAKA")
    hoods.CreateField(yaka)
    hoods.CreateField(ogr.FieldDefn("KIMLIKNO", ogr.OFTInteger))
    hoods.CreateField(ad)
    rows = [(1421, 1, 40001, "FİKİRTEPE"), (1421, 1, 40002, "HASANPAŞA"), (1103, 1, None, "ORMAN")]
    for i, (district, side, ident, label) in enumerate(rows):
        f = ogr.Feature(hoods.GetLayerDefn())
        f.SetField("ILCEKN", district)
        f.SetField("YAKA", side)
        if ident is not None:
            f.SetField("KIMLIKNO", ident)
        f.SetField("AD", label)
        f.SetGeometry(_square(500000 + i * 100, 4500000))
        hoods.CreateFeature(f)

    aux = ds.CreateLayer("Aux_Boundary", srs, ogr.wkbPolygon, opts)
    f = ogr.Feature(aux.GetLayerDefn())
    f.SetGeometry(_square(500000, 4500000, 400))
    aux.CreateFeature(f)
    ds = None


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_manifest(root: Path) -> None:
    files = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and rel not in ("PACKAGE/MANIFEST.json", "PACKAGE/SHA256SUMS.csv") and p.suffix != ".lock":
            files.append({"path": rel, "bytes": p.stat().st_size, "sha256": _sha(p)})
    manifest = {
        "built_at_utc": "2026-11-12T11:31:43+00:00",
        "file_count": len(files),
        "package_id": "TT01_ADMIN_BOUNDARIES_v1.0.0-rc1",
        "package_type": "controlled_source_transfer_qa_conditional",
        "source_path_on_origin_machine": "E:\\TEST\\Admin.gdb",
        "status": "READY_FOR_CONTROLLED_TRANSFER__QA_CONDITIONAL",
        "sha256_files": files,
    }
    (root / "PACKAGE" / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def build_package(root: Path, *, counts: dict[str, int] | None = None, null_oids: list[int] | None = None,
                  version: str = "1.0.0-rc1", release_overrides: dict | None = None,
                  extra_register_rows: list[list] | None = None, passport: dict | None = None,
                  layer_metadata: dict[str, dict] | None = None) -> Path:
    pkg = root / "PACKAGE"
    (pkg / "QA").mkdir(parents=True)
    src = root / "01_ADMIN" / "SOURCE"
    (src / "Admin.metadata" / "EVIDENCE").mkdir(parents=True)
    _build_gdb(src / "Admin.gdb")

    counts = counts or {"Districts": 2, "Neighborhoods": 3, "Aux_Boundary": 1}
    release = {
        "dataset_id": "THERMALIS_DATA/TT01_ADMIN_BOUNDARIES/ISTANBUL",
        "version": version,
        "status": "READY_FOR_CONTROLLED_TRANSFER__QA_CONDITIONAL",
        "core_dataset_names": ["Districts", "Neighborhoods"],
        "auxiliary_dataset_names_not_for_primary_publication": ["Aux_Boundary"],
        "data_owner": "Test Kurumu",
        "not_released_as_public_open_data": True,
        "transfer_authorization": "test",
        "steward_topology_disposition": "OPEN",
    }
    release.update(release_overrides or {})
    release = {k: v for k, v in release.items() if v is not None}
    (pkg / "RELEASE_STATUS.json").write_text(json.dumps(release), encoding="utf-8")
    inventory = {k: {"count": v, **({"metadata": layer_metadata[k]} if layer_metadata and k in layer_metadata else {})} for k, v in counts.items()}
    (pkg / "DATASET_INVENTORY_AND_SCHEMA.json").write_text(json.dumps(inventory, ensure_ascii=False), encoding="utf-8")
    if passport is not None:
        (src / "Admin.metadata" / "TT01_DATASET_PASSPORT.json").write_text(json.dumps(passport, ensure_ascii=False), encoding="utf-8")
    (pkg / "CODED_DOMAINS.json").write_text(json.dumps([
        {"name": "Ilce_UAVT", "description": "UAVT İlçe Kodları", "coded_values": ILCE_UAVT},
        {"name": "YAKA", "description": "Yaka", "coded_values": YAKA},
    ], ensure_ascii=False), encoding="utf-8")
    (pkg / "CRS_AND_UNITS.json").write_text(json.dumps({"native_crs_name": "ITRF_96_UTM_Zone_35N", "native_wkt": ESRI_WKT, "factory_code": 0, "unit": "metre"}), encoding="utf-8")
    dictionary = [
        ("Districts", "OBJECTID", "OID", ""), ("Districts", "Shape", "Geometry", ""), ("Districts", "ILKN", "Integer", ""),
        ("Districts", "KIMLIKNO", "Integer", ""), ("Districts", "AD", "String", ""),
        ("Districts", "Shape_Length", "Double", ""), ("Districts", "Shape_Area", "Double", ""),
        ("Neighborhoods", "OBJECTID", "OID", ""), ("Neighborhoods", "Shape", "Geometry", ""),
        ("Neighborhoods", "ILCEKN", "Integer", "Ilce_UAVT"), ("Neighborhoods", "YAKA", "SmallInteger", "YAKA"),
        ("Neighborhoods", "KIMLIKNO", "Integer", ""), ("Neighborhoods", "AD", "String", ""),
        ("Neighborhoods", "Shape_Length", "Double", ""), ("Neighborhoods", "Shape_Area", "Double", ""),
        ("Aux_Boundary", "OBJECTID", "OID", ""), ("Aux_Boundary", "Shape", "Geometry", ""),
        ("Aux_Boundary", "Shape_Length", "Double", ""), ("Aux_Boundary", "Shape_Area", "Double", ""),
    ]
    with (pkg / "DATA_DICTIONARY.csv").open("w", encoding="utf-8-sig", newline="") as h:
        w = csv.writer(h)
        w.writerow(["dataset", "field", "alias", "type", "length", "nullable", "required", "coded_domain"])
        for ds, fld, typ, dom in dictionary:
            w.writerow([ds, fld, fld, typ, 4, True, False, dom])
    with (pkg / "QA" / "TT01_EXCEPTION_REGISTER.csv").open("w", encoding="utf-8-sig", newline="") as h:
        w = csv.writer(h)
        w.writerow(["exception_id", "category", "dataset", "feature_a", "oid_a", "feature_b", "oid_b", "area_m2", "percent_of_feature", "evidence", "qa_action", "disposition"])
        w.writerow(["TT01-DIST-OV-01", "DISTRICT_OVERLAP", "Districts", "ADALAR", 1, "KADIKÖY", 2, "3.5", "", "x", "Do not alter", "OPEN_OWNER_REVIEW"])
        w.writerow(["TT01-NB-OUT-01", "NEIGHBORHOOD_OUTSIDE_PARENT", "Neighborhoods", "FİKİRTEPE", 1, "KADIKÖY", 2, "61.1", "0.67", "x", "Do not alter", "OPEN_OWNER_REVIEW"])
        for row in extra_register_rows or []:
            w.writerow(row)
    (pkg / "QA" / "TT01_GATES.json").write_text(json.dumps({"gates": [{"id": "G02", "status": "HOLD", "detail": "steward"}],
                                                             "auxiliary": {"Aux_Boundary": "SELF_INTERSECTION_EXCLUDE_FROM_CORE"}}), encoding="utf-8")
    null_oids = [3] if null_oids is None else null_oids
    (src / "Admin.metadata" / "EVIDENCE" / "KEYS.json").write_text(json.dumps({"layers": {"Neighborhoods": {
        "keys": {"KIMLIKNO": {"null_count": len(null_oids)}, "ILCEKN": {"null_count": 0}},
        "missing_identity_records": [{"OBJECTID": o, "AD": "ORMAN", "ILCEKN": 1103} for o in null_oids],
    }}}, ensure_ascii=False), encoding="utf-8")
    (pkg / "README.md").write_text("# Test paketi\n", encoding="utf-8")
    write_manifest(root)
    (pkg / "SHA256SUMS.csv").write_text("relative_path,bytes,sha256\n", encoding="utf-8")
    return root


def write_ready_marker(root: Path, sha: str | None = None) -> None:
    """Teslim protokolü: kopyalama bittiğinde paket köküne manifest özetiyle hazır işareti yazılır."""
    digest = sha or _sha(root / "PACKAGE" / "MANIFEST.json")
    (root / "THERMALIS_READY.json").write_text(json.dumps({"manifest_sha256": digest}), encoding="utf-8")


@pytest.fixture()
def package(tmp_path: Path) -> Path:
    return build_package(tmp_path / "TT01_ADMIN_BOUNDARIES_v1.0.0-rc1")


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "THERMALIS_DATA"
    root.mkdir()
    return root
