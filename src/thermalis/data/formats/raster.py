"""Genel raster/COG başlık incelemesi (GDAL; yalnız okuma).

Yalnız başlık ve metadata okunur (piksel istatistiği hesaplanmaz). Yol yerel dosya, ``/vsisubfile/`` (arşiv
üyesi) veya ``/vsis3/`` (bulut nesnesi) olabilir: aynı fonksiyon üçünde de aynı sonucu verir.

Kayıt kuralları:
- Değerler dosyada GÖMÜLÜ olduğu gibi kaydedilir. Ölçek/kaydırma gömülü değilse ``None`` (uydurulmaz).
- CRS yalnız dosyadaki tanımdan; EPSG kodu yalnız yetki kodu açıkça varsa.
- Coğrafi kapsam (WGS84) köşe noktalarının dönüşümüyle hesaplanır ve böyle etiketlenir (yaklaşık sınır kutusu).
"""

from __future__ import annotations

import hashlib
from typing import Any

RASTER_SUFFIXES = (".tif", ".tiff", ".gtif", ".jp2", ".img", ".vrt")


def gdal_available() -> bool:
    try:
        from osgeo import gdal  # noqa: F401
    except ImportError:  # pragma: no cover
        return False
    return True


def driver_available(name: str) -> bool:
    try:
        from osgeo import gdal
    except ImportError:  # pragma: no cover
        return False
    return gdal.GetDriverByName(name) is not None


def inspect_raster(path: str) -> dict[str, Any]:
    """Raster başlık bilgisi. Açılamazsa ``{"readable": False, "error": ...}`` (istisna fırlatmaz)."""
    from osgeo import gdal, osr

    gdal.UseExceptions()
    try:
        ds = gdal.OpenEx(path, gdal.OF_RASTER | gdal.OF_READONLY)
    except RuntimeError as exc:
        return {"readable": False, "error": str(exc)[:300]}
    if ds is None:  # pragma: no cover
        return {"readable": False, "error": "gdal_open_returned_none"}
    try:
        gt = ds.GetGeoTransform(can_return_null=True)
        srs = ds.GetSpatialRef()
        bands = []
        for i in range(1, ds.RasterCount + 1):
            b = ds.GetRasterBand(i)
            block = b.GetBlockSize()
            bands.append({
                # GDAL ölçek/kaydırma tanımlı değilse None döndürür: gömülü değer yok demektir (1/0 varsayılmaz).
                "index": i, "dtype": gdal.GetDataTypeName(b.DataType), "nodata": b.GetNoDataValue(),
                "embedded_scale": b.GetScale(), "embedded_offset": b.GetOffset(),
                "unit_type": b.GetUnitType() or None, "description": b.GetDescription() or None,
                "color_interpretation": gdal.GetColorInterpretationName(b.GetColorInterpretation()),
                "block": [int(block[0]), int(block[1])], "overviews": b.GetOverviewCount(),
            })
        crs: dict[str, Any] = {"defined": srs is not None, "epsg": None, "name": None, "wkt_sha256": None}
        if srs is not None:
            code, auth = srs.GetAuthorityCode(None), srs.GetAuthorityName(None)
            wkt = srs.ExportToWkt()
            vertical = None
            if srs.IsCompound() or srs.IsVertical():
                vertical = srs.GetAttrValue("VERT_CS") or srs.GetAttrValue("VERTCRS") or "declared_in_crs"
            crs.update(epsg=int(code) if code and auth == "EPSG" else None, name=srs.GetName(), wkt=wkt,
                       vertical_crs=vertical,  # yalnız CRS'de gömülüyse (ör. "EGM2008 height"); yoksa None = beyan yok
                       wkt_sha256=hashlib.sha256(wkt.encode("utf-8")).hexdigest(),
                       units=srs.GetLinearUnitsName() if srs.IsProjected() else srs.GetAngularUnitsName())
        out: dict[str, Any] = {
            "readable": True, "driver": ds.GetDriver().ShortName, "width": ds.RasterXSize, "height": ds.RasterYSize,
            "band_count": ds.RasterCount, "bands": bands, "crs": crs,
            "geotransform": list(gt) if gt else None,
            "resolution": [abs(gt[1]), abs(gt[5])] if gt else None,
            "rotated": bool(gt and (gt[2] or gt[4])),
            "compression": ds.GetMetadataItem("COMPRESSION", "IMAGE_STRUCTURE"),
            "interleave": ds.GetMetadataItem("INTERLEAVE", "IMAGE_STRUCTURE"),
            "layout": ds.GetMetadataItem("LAYOUT", "IMAGE_STRUCTURE"),
        }
        tiled = bool(bands) and bands[0]["block"][1] > 1 and bands[0]["block"][0] < ds.RasterXSize
        out["tiled"] = tiled
        # COG: GDAL ≥3.1 başlıktaki COG düzen işaretini LAYOUT=COG olarak bildirir. Yoksa bilinmiyor (yanlış "hayır" değil).
        out["cloud_optimized"] = True if (out["layout"] or "").upper() == "COG" else (None if tiled else False)
        if gt and not out["rotated"]:
            xs = [gt[0], gt[0] + gt[1] * ds.RasterXSize]
            ys = [gt[3], gt[3] + gt[5] * ds.RasterYSize]
            out["extent"] = {"minx": min(xs), "miny": min(ys), "maxx": max(xs), "maxy": max(ys)}
            if srs is not None:
                try:
                    wgs = osr.SpatialReference()
                    wgs.ImportFromEPSG(4326)
                    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                    src = srs.Clone()
                    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                    tr = osr.CoordinateTransformation(src, wgs)
                    pts = [tr.TransformPoint(x, y)[:2] for x in xs for y in ys]
                    out["bbox_wgs84"] = [min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts),
                                         max(p[1] for p in pts)]
                    out["bbox_wgs84_method"] = "corner_transform_approximate"
                except RuntimeError:
                    out["bbox_wgs84"] = None
        return out
    finally:
        ds = None


def read_window(path: str, band: int, xoff: int, yoff: int, xsize: int, ysize: int) -> bytes:
    """Kaynak değerleri (ham DN) değiştirmeden okur — doğrulama/karşılaştırma için."""
    from osgeo import gdal

    gdal.UseExceptions()
    ds = gdal.Open(path)
    try:
        return ds.GetRasterBand(band).ReadRaster(xoff, yoff, xsize, ysize)
    finally:
        ds = None
