"""Vektör kaynakların salt okunur incelenmesi (GDAL/OGR).

Hiçbir dönüşüm, onarım veya yazma yapılmaz. File Geodatabase yalnız
``OpenFileGDB`` sürücüsüyle ve ``OF_READONLY`` bayrağıyla açılır.
"""

from __future__ import annotations

import hashlib
import json
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


def inspect_vector(path: Path, expected_wkt: str | None = None) -> dict[str, Any]:
    """Katmanları, kayıt sayılarını (tek tek okunarak), alanları, boş değerleri, CRS'yi ve domainleri döndürür.

    Dönen sözlükte GDAL nesnesi yoktur; veri kaynağı fonksiyon bitmeden kapatılır
    (Windows'ta açık dosya, klasörün taşınmasını engeller).
    """
    ds = _open(path)
    try:
        return _inspect(ds, expected_wkt)
    finally:
        ds = None  # noqa: F841 - kaynağı kapat


def _inspect(ds, expected_wkt: str | None) -> dict[str, Any]:
    from osgeo import ogr

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
        feature = geom = None  # GDAL nesnelerine referans bırakma
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
            "crs_wkt": srs.ExportToWkt() if srs is not None else None,
            "crs_matches_expected": (crs_matches(srs, expected_wkt) if expected_wkt else None) if srs is not None else False,
        }
        srs = None
        layer = None

    domains: dict[str, Any] = {}
    for name in ds.GetFieldDomainNames() or []:
        domain = ds.GetFieldDomain(name)
        entry: dict[str, Any] = {"description": domain.GetDescription(), "type": domain.GetDomainType()}
        if domain.GetDomainType() == ogr.OFDT_CODED:
            entry["coded_values"] = {str(k): v for k, v in domain.GetEnumeration().items()}
        domains[name] = entry
    return {"layers": layers, "domains": domains}


CONTENT_ID_ALGORITHM = "lc1-sha256-multiset"
_MOD = 1 << 256


def _canonical(value: Any) -> Any:
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def layer_facts(path: Path, layer_name: str) -> dict[str, Any]:
    """Katmanın kanıtlanabilir içerik kimliği ve yapısal bilgileri (salt okunur, akış hâlinde).

    İçerik kimliği; şema (alan adı, tipi, alt tipi, genişliği, boş olabilirliği, domain adı),
    kullanılan domainlerin kod tabloları, CRS WKT'si, geometri tipi ve her kaydın öznitelik +
    geometri (ISO WKB) özetinden oluşur. Kayıt özetleri sıradan bağımsız biçimde toplanır
    (SHA-256 değerlerinin 2^256 modunda toplamı + kayıt sayısı), böylece bellek kullanımı kayıt
    sayısıyla büyümez. OBJECTID (FID) kimliğe girmez: taşınabilir anlam taşımaz.
    """
    from osgeo import ogr

    ds = _open(path)
    try:
        layer = ds.GetLayerByName(layer_name)
        if layer is None:
            raise KeyError(layer_name)
        defn = layer.GetLayerDefn()
        fields = []
        for i in range(defn.GetFieldCount()):
            fd = defn.GetFieldDefn(i)
            fields.append({"name": fd.GetName(), "type": fd.GetType(), "subtype": fd.GetSubType(), "width": fd.GetWidth(),
                           "nullable": bool(fd.IsNullable()), "domain": fd.GetDomainName() or None})
        domains = {}
        for f in fields:
            if f["domain"] and f["domain"] not in domains:
                dom = ds.GetFieldDomain(f["domain"])
                domains[f["domain"]] = sorted((str(k), v) for k, v in (dom.GetEnumeration() or {}).items()) if dom and dom.GetDomainType() == ogr.OFDT_CODED else None
        srs = layer.GetSpatialRef()
        header = json.dumps({"algorithm": CONTENT_ID_ALGORITHM, "fields": fields, "domains": domains,
                             "geometry_type": ogr.GeometryTypeToName(layer.GetGeomType()),
                             "crs_wkt": srs.ExportToWkt() if srs is not None else None}, sort_keys=True, ensure_ascii=False)
        total, count = 0, 0
        layer.ResetReading()
        geometry_of = _geometry_accessor()
        for feature in layer:
            values = [_canonical(feature.GetField(i)) if feature.IsFieldSetAndNotNull(i) else None for i in range(len(fields))]
            geom = geometry_of(feature)
            digest = hashlib.sha256(json.dumps(values, ensure_ascii=False, default=str).encode("utf-8") + b"\x00" +
                                    (bytes(geom.ExportToIsoWkb()) if geom is not None else b"")).digest()
            total = (total + int.from_bytes(digest, "big")) % _MOD
            count += 1
        feature = geom = None
        content_id = hashlib.sha256(f"{header}\n{count}\n{total:064x}".encode("utf-8")).hexdigest()
        result = {"content_id": content_id, "algorithm": CONTENT_ID_ALGORITHM, "feature_count": count,
                  "schema_sha256": hashlib.sha256(header.encode("utf-8")).hexdigest(),
                  "fid_column": layer.GetFIDColumn() or None, "geometry_column": layer.GetGeometryColumn() or None}
        srs = layer = None
        return result
    finally:
        ds = None  # noqa: F841


def _geometry_accessor():
    """Geometri okuyucusu (yalnız akış içinde, nesne hemen kullanılırken).

    GDAL Python bağlamalarının ``Feature.GetGeometryRef`` yöntemi her nesneye geometri izlemek için bir ``WeakSet``
    ekler; bu, nesne başına çöp toplayıcıya bağlı geçici bellek üretir ve tepe bellek önceki yığın durumuna göre
    ikiye katlanabilir. İçerik kimliği hesabında geometri nesne canlıyken hemen WKB'ye çevrildiği için izleme
    gerekmez: doğrudan erişimci kullanılır (aynı geometri, aynı WKB). Erişimci yoksa genel yöntem kullanılır.
    """
    from osgeo import ogr

    raw = getattr(getattr(ogr, "_ogr", None), "Feature_GetGeometryRef", None)
    return raw if raw is not None else (lambda feature: feature.GetGeometryRef())


def read_records(path: Path, layer_name: str, fids: list[int], fields: list[str]) -> dict[int, dict[str, Any]]:
    """Belirli FID'lerin (OBJECTID) yalnız istenen alanlarını salt okunur okur; olmayan alan ``None`` döner."""
    ds = _open(path)
    try:
        layer = ds.GetLayerByName(layer_name)
        if layer is None:
            return {}
        defn = layer.GetLayerDefn()
        index = {f: defn.GetFieldIndex(f) for f in fields}
        out: dict[int, dict[str, Any]] = {}
        for fid in fids:
            feature = layer.GetFeature(int(fid))
            if feature is None:
                continue
            out[int(fid)] = {f: (feature.GetField(i) if i >= 0 and feature.IsFieldSetAndNotNull(i) else None) for f, i in index.items()}
        feature = layer = None
        return out
    finally:
        ds = None  # noqa: F841


def null_field_fids(path: Path, layer_name: str, field_name: str) -> list[int]:
    """Bir alanı boş olan kayıtların FID (OBJECTID) listesi."""
    ds = _open(path)
    try:
        layer = ds.GetLayerByName(layer_name)
        index = layer.GetLayerDefn().GetFieldIndex(field_name)
        result = []
        layer.ResetReading()
        for feature in layer:
            if not feature.IsFieldSetAndNotNull(index):
                result.append(feature.GetFID())
        feature = layer = None
        return sorted(result)
    finally:
        ds = None  # noqa: F841
