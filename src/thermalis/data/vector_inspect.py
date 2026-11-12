"""Vektör kaynakların salt okunur incelenmesi (GDAL/OGR).

Hiçbir dönüşüm, onarım veya yazma yapılmaz. File Geodatabase yalnız
``OpenFileGDB`` sürücüsüyle ve ``OF_READONLY`` bayrağıyla açılır.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

FIELD_TYPE_NAMES = {
    0: "Integer", 1: "IntegerList", 2: "Real", 3: "RealList", 4: "String", 5: "StringList",
    8: "Binary", 9: "Date", 10: "Time", 11: "DateTime", 12: "Integer64", 13: "Integer64List",
}


def gdal_available() -> tuple[bool, str]:
    try:
        from osgeo import gdal  # noqa: F401
    except Exception as exc:  # pragma: no cover - ortam bağımlı
        return False, f"GDAL import failed: {exc}"
    return True, gdal.__version__


def _open(path: Path):
    from osgeo import gdal

    gdal.UseExceptions()
    return gdal.OpenEx(str(path), gdal.OF_VECTOR | gdal.OF_READONLY, allowed_drivers=["OpenFileGDB"])


def normalize_esri_wkt(wkt: str) -> str:
    """ArcGIS'in WKT sonuna eklediği hassasiyet/tolerans kısmını (';...') ayırır."""
    return wkt.split(";", 1)[0].strip()


def crs_matches(layer_srs, expected_wkt: str) -> bool:
    from osgeo import osr

    expected = osr.SpatialReference()
    expected.ImportFromWkt(normalize_esri_wkt(expected_wkt))
    return bool(layer_srs is not None and layer_srs.IsSame(expected))


def inspect_vector(path: Path) -> dict[str, Any]:
    """Katmanları, kayıt sayılarını (tek tek okunarak), alanları, boş değerleri, CRS'yi ve domainleri döndürür."""
    from osgeo import ogr

    ds = _open(path)
    layers: dict[str, Any] = {}
    for index in range(ds.GetLayerCount()):
        layer = ds.GetLayerByIndex(index)
        defn = layer.GetLayerDefn()
        fields = []
        for i in range(defn.GetFieldCount()):
            fd = defn.GetFieldDefn(i)
            fields.append(
                {
                    "name": fd.GetName(),
                    "alias": fd.GetAlternativeName() or None,
                    "type": FIELD_TYPE_NAMES.get(fd.GetType(), str(fd.GetType())),
                    "width": fd.GetWidth(),
                    "nullable": bool(fd.IsNullable()),
                    "domain": fd.GetDomainName() or None,
                }
            )
        null_counts = {f["name"]: 0 for f in fields}
        counted = 0
        null_geometry = 0
        empty_geometry = 0
        layer.ResetReading()
        for feature in layer:
            counted += 1
            for i, f in enumerate(fields):
                if not feature.IsFieldSetAndNotNull(i):
                    null_counts[f["name"]] += 1
            geom = feature.GetGeometryRef()
            if geom is None:
                null_geometry += 1
            elif geom.IsEmpty():
                empty_geometry += 1
        srs = layer.GetSpatialRef()
        extent = None
        if counted and layer.GetGeomType() != ogr.wkbNone:
            minx, maxx, miny, maxy = layer.GetExtent()
            extent = {"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy}
        layers[layer.GetName()] = {
            "geometry_type": ogr.GeometryTypeToName(layer.GetGeomType()),
            "feature_count": counted,
            "header_feature_count": layer.GetFeatureCount(),
            "fid_column": layer.GetFIDColumn() or None,
            "fields": fields,
            "null_counts": null_counts,
            "null_geometry": null_geometry,
            "empty_geometry": empty_geometry,
            "extent": extent,
            "crs_name": srs.GetName() if srs is not None else None,
            "_srs": srs,
        }

    domains: dict[str, Any] = {}
    for name in ds.GetFieldDomainNames() or []:
        domain = ds.GetFieldDomain(name)
        entry: dict[str, Any] = {"description": domain.GetDescription(), "type": domain.GetDomainType()}
        if domain.GetDomainType() == ogr.OFDT_CODED:
            entry["coded_values"] = {str(k): v for k, v in domain.GetEnumeration().items()}
        domains[name] = entry
    return {"layers": layers, "domains": domains, "_dataset": ds}


def null_field_fids(path: Path, layer_name: str, field_name: str) -> list[int]:
    """Bir alanı boş olan kayıtların FID (OBJECTID) listesi."""
    ds = _open(path)
    layer = ds.GetLayerByName(layer_name)
    index = layer.GetLayerDefn().GetFieldIndex(field_name)
    result = []
    layer.ResetReading()
    for feature in layer:
        if not feature.IsFieldSetAndNotNull(index):
            result.append(feature.GetFID())
    return sorted(result)
