"""Landsat Collection 2 Level-2 (L2SP/L2SR) metadata YORUMLAYICISI.

Genel TAR/raster bağdaştırıcısından ayrıdır: arşiv dizini ve raster başlıkları biçimden bağımsız üretilir; bu modül
yalnız ürünle gelen metadata belgelerini (``*_MTL.json``, ``*_SR_stac.json``, ``*_ST_stac.json``) okuyup sahne
özelliklerini ve bant rollerini KAYNAKTA BEYAN EDİLDİĞİ GİBİ çıkarır.

Kurallar:
- Ölçek/kaydırma yalnız MTL'de açıkça yazılmışsa kaydedilir (kaynak anahtarıyla); UYGULANMAZ. Yazılmamışsa ``None``
  ve ``scale_status = "not_declared_in_source_metadata"``.
- Birim MTL'de beyan edilmez: ``unit = None`` (USGS belgelerinden varsayım yapılmaz).
- STAC sınıflandırma bit alanları (QA bantları) olduğu gibi saklanır.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

INTERPRETER = "landsat_c2_l2/1"
PRODUCT_RE = re.compile(r"^(L[COTEM]\d{2})_(L2S[PR])_(\d{3})(\d{3})_(\d{8})_(\d{8})_(\d{2})_(T1|T2|RT)$")
_SR_BAND = re.compile(r"^SR_B(\d+)$")
_ST_BAND = re.compile(r"^ST_B(\d+)$")


def product_id_of(member_name: str) -> str | None:
    base = member_name.rsplit("/", 1)[-1]
    m = re.match(r"^(L[COTEM]\d{2}_L2S[PR]_\d{6}_\d{8}_\d{8}_\d{2}_(?:T1|T2|RT))_", base)
    return m.group(1) if m else None


def detect(member_names: list[str]) -> bool:
    ids = {product_id_of(n) for n in member_names}
    return len(ids - {None}) == 1 and any(n.endswith("_MTL.json") for n in member_names)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _band_code(member_name: str, product_id: str) -> str:
    base = member_name.rsplit("/", 1)[-1]
    rest = base[len(product_id) + 1:] if base.startswith(product_id + "_") else base
    return rest.rsplit(".", 1)[0] if rest.upper().endswith((".TIF", ".TIFF")) else rest


def _scale_for(code: str, mtl: dict[str, Any]) -> dict[str, Any]:
    sr = mtl.get("LEVEL2_SURFACE_REFLECTANCE_PARAMETERS") or {}
    st = mtl.get("LEVEL2_SURFACE_TEMPERATURE_PARAMETERS") or {}
    m = _SR_BAND.match(code)
    if m and f"REFLECTANCE_MULT_BAND_{m.group(1)}" in sr:
        n = m.group(1)
        return {"scale": _float(sr.get(f"REFLECTANCE_MULT_BAND_{n}")), "add_offset": _float(sr.get(f"REFLECTANCE_ADD_BAND_{n}")),
                "scale_source": f"MTL.json LEVEL2_SURFACE_REFLECTANCE_PARAMETERS.REFLECTANCE_MULT_BAND_{n}",
                "add_offset_source": f"MTL.json LEVEL2_SURFACE_REFLECTANCE_PARAMETERS.REFLECTANCE_ADD_BAND_{n}",
                "source_declared_dn_range": [_float(sr.get(f"QUANTIZE_CAL_MIN_BAND_{n}")), _float(sr.get(f"QUANTIZE_CAL_MAX_BAND_{n}"))],
                "source_declared_value_range": [_float(sr.get(f"REFLECTANCE_MINIMUM_BAND_{n}")),
                                                _float(sr.get(f"REFLECTANCE_MAXIMUM_BAND_{n}"))],
                "quantity": "surface_reflectance", "scale_status": "source_declared"}
    m = _ST_BAND.match(code)
    if m and f"TEMPERATURE_MULT_BAND_ST_B{m.group(1)}" in st:
        n = m.group(1)
        return {"scale": _float(st.get(f"TEMPERATURE_MULT_BAND_ST_B{n}")), "add_offset": _float(st.get(f"TEMPERATURE_ADD_BAND_ST_B{n}")),
                "scale_source": f"MTL.json LEVEL2_SURFACE_TEMPERATURE_PARAMETERS.TEMPERATURE_MULT_BAND_ST_B{n}",
                "add_offset_source": f"MTL.json LEVEL2_SURFACE_TEMPERATURE_PARAMETERS.TEMPERATURE_ADD_BAND_ST_B{n}",
                "source_declared_dn_range": [_float(st.get(f"QUANTIZE_CAL_MINIMUM_BAND_ST_B{n}")),
                                             _float(st.get(f"QUANTIZE_CAL_MAXIMUM_BAND_ST_B{n}"))],
                "source_declared_value_range": [_float(st.get(f"TEMPERATURE_MINIMUM_BAND_ST_B{n}")),
                                                _float(st.get(f"TEMPERATURE_MAXIMUM_BAND_ST_B{n}"))],
                "quantity": "surface_temperature", "scale_status": "source_declared"}
    return {"scale": None, "add_offset": None, "scale_source": None, "add_offset_source": None,
            "scale_status": "not_declared_in_source_metadata"}


def interpret(member_names: list[str], read_member: Callable[[str], bytes]) -> dict[str, Any]:
    """Sahne kaydı. ``read_member(ad)`` küçük metadata üyelerini okur (MTL/STAC; birkaç on KB)."""
    product_id = next(p for p in (product_id_of(n) for n in member_names) if p)
    mtl_name = next(n for n in member_names if n.endswith("_MTL.json"))
    mtl = json.loads(read_member(mtl_name).decode("utf-8"))["LANDSAT_METADATA_FILE"]
    pc, ia = mtl.get("PRODUCT_CONTENTS") or {}, mtl.get("IMAGE_ATTRIBUTES") or {}
    pa, l2 = mtl.get("PROJECTION_ATTRIBUTES") or {}, mtl.get("LEVEL2_PROCESSING_RECORD") or {}
    l1 = mtl.get("LEVEL1_PROCESSING_RECORD") or {}
    issues: list[str] = []
    if pc.get("LANDSAT_PRODUCT_ID") != product_id:
        issues.append(f"mtl_product_id_differs_from_member_names:{pc.get('LANDSAT_PRODUCT_ID')}")

    stac: dict[str, dict[str, Any]] = {}
    stac_props: dict[str, Any] = {}
    stac_bbox = None
    stac_members = sorted(n for n in member_names if n.endswith("_stac.json"))
    for name in stac_members:
        try:
            doc = json.loads(read_member(name).decode("utf-8"))
        except ValueError:
            issues.append(f"stac_unreadable:{name}")
            continue
        stac_props.update(doc.get("properties") or {})
        stac_bbox = stac_bbox or doc.get("bbox")
        for key, asset in (doc.get("assets") or {}).items():
            href = str(asset.get("href") or "").rsplit("/", 1)[-1]
            if href and href not in stac:
                stac[href] = {"stac_key": key, **{k: asset[k] for k in ("title", "description", "roles", "type", "eo:bands",
                                                                           "classification:bitfields") if k in asset}}

    date, centre = ia.get("DATE_ACQUIRED"), ia.get("SCENE_CENTER_TIME")
    properties = {
        "product_id": product_id, "scene_id": l1.get("LANDSAT_SCENE_ID") or stac_props.get("landsat:scene_id"),
        "platform": ia.get("SPACECRAFT_ID"), "sensor": ia.get("SENSOR_ID"),
        "instruments": stac_props.get("instruments"),
        "wrs_type": ia.get("WRS_TYPE"), "wrs_path": ia.get("WRS_PATH"), "wrs_row": ia.get("WRS_ROW"),
        "acquisition_date": date, "scene_center_time": centre,
        "datetime": stac_props.get("datetime") or (f"{date}T{centre}" if date and centre else None),
        "cloud_cover": _float(ia.get("CLOUD_COVER")), "cloud_cover_land": _float(ia.get("CLOUD_COVER_LAND")),
        "image_quality_oli": ia.get("IMAGE_QUALITY_OLI"), "image_quality_tirs": ia.get("IMAGE_QUALITY_TIRS"),
        "sun_azimuth": _float(ia.get("SUN_AZIMUTH")), "sun_elevation": _float(ia.get("SUN_ELEVATION")),
        "collection_number": pc.get("COLLECTION_NUMBER"), "collection_category": pc.get("COLLECTION_CATEGORY"),
        "processing_level": pc.get("PROCESSING_LEVEL"), "doi": pc.get("DIGITAL_OBJECT_IDENTIFIER"),
        "date_product_generated": l2.get("DATE_PRODUCT_GENERATED"),
        "processing_software_version": l2.get("PROCESSING_SOFTWARE_VERSION"),
        "algorithm_surface_temperature": l2.get("ALGORITHM_SOURCE_SURFACE_TEMPERATURE"),
        "algorithm_surface_reflectance": l2.get("ALGORITHM_SOURCE_SURFACE_REFLECTANCE"),
        "map_projection": pa.get("MAP_PROJECTION"), "utm_zone": pa.get("UTM_ZONE"), "datum": pa.get("DATUM"),
        "grid_cell_size_reflective": _float(pa.get("GRID_CELL_SIZE_REFLECTIVE")),
        "grid_cell_size_thermal": _float(pa.get("GRID_CELL_SIZE_THERMAL")),
        "proj_epsg": stac_props.get("proj:epsg"),
        "geometric_rmse": stac_props.get("accuracy:geometric_rmse"),
    }
    corners = [(_float(pa.get(f"CORNER_{c}_LON_PRODUCT")), _float(pa.get(f"CORNER_{c}_LAT_PRODUCT"))) for c in ("UL", "UR", "LL", "LR")]
    corners_ok = all(x is not None and y is not None for x, y in corners)

    assets: dict[str, dict[str, Any]] = {}
    for name in member_names:
        base = name.rsplit("/", 1)[-1]
        code = _band_code(name, product_id)
        info = stac.get(base, {})
        entry: dict[str, Any] = {"band_code": code, "stac": info or None, "unit": None, "unit_status": "not_declared_in_source_metadata"}
        if base.upper().endswith((".TIF", ".TIFF")):
            entry.update(_scale_for(code, mtl))
            entry["role"] = ("quality" if code.startswith("QA_") or code in ("ST_QA", "SR_QA_AEROSOL") else
                             "data" if _SR_BAND.match(code) or _ST_BAND.match(code) else "auxiliary")
        else:
            entry["role"] = "metadata" if code.startswith(("MTL", "ANG")) or code.endswith("stac") else (
                "thumbnail" if "thumb" in code else "other")
        assets[name] = entry
    return {
        "interpreter": INTERPRETER, "item_id": product_id, "properties": properties,
        "bbox_wgs84": stac_bbox, "bbox_source": "STAC bbox" if stac_bbox else None,
        "footprint_wgs84": [[x, y] for x, y in corners] if corners_ok else None,
        "footprint_source": "MTL.json PROJECTION_ATTRIBUTES CORNER_*_PRODUCT" if corners_ok else None,
        "assets": assets, "metadata_members": {"mtl": mtl_name, "stac": stac_members}, "issues": issues,
    }


INTERPRETERS = {"landsat_c2_l2": (detect, interpret, INTERPRETER)}
