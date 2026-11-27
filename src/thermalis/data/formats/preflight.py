"""Biçim ön denetimi: yalnız dosya listesinden (yol + boyut), indirme veya kopya yapmadan.

Bulut kabulü bu denetimi paketi İNDİRMEDEN ÖNCE yapar: desteklenmeyen biçimli paket indirilmez, hazırlık alanı
harcanmaz ve kalıcı RED yazılmaz; paket ``ADAPTER_REQUIRED`` olarak ERTELENİR (bağdaştırıcı eklenince yeniden denenir).

Rotalar:
- ``vector_package``      File Geodatabase içeren mevcut paket sözleşmesi (RELEASE_STATUS.json) → mevcut kayıt iş akışı
- ``raster_collection``   TAR/GeoTIFF kaynakları → paket dışı beyan + doğrudan yayın (yerel tam kopya yok)
- ``zip_bundle``          ZIP aktarım paketi (arşiv + SHA-256 yan dosyası + iç manifest) → beyan + doğrudan yayın
- ``None``                desteklenmeyen biçim → bağdaştırıcı gerekli
"""

from __future__ import annotations

import collections
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Iterable

DOCUMENT_SUFFIXES = (".json", ".csv", ".md", ".txt", ".pdf", ".xml", ".html", ".htm", ".yaml", ".yml", ".jpeg", ".jpg", ".png")
# GDAL sürücüsü gerektiren bilimsel biçimler: sürücü bu kurulumda yoksa bağdaştırıcı gerekli.
DRIVER_FORMATS = {".nc": "netCDF", ".nc4": "netCDF", ".h5": "HDF5", ".hdf5": "HDF5", ".he5": "HDF5", ".hdf": "HDF4",
                  ".grib": "GRIB", ".grb": "GRIB", ".grib2": "GRIB"}
COMPRESSED_TAR = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar.zst")


@dataclass
class Preflight:
    route: str | None
    formats: dict[str, int] = field(default_factory=dict)
    unsupported: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    data_files: int = 0

    @property
    def supported(self) -> bool:
        return self.route is not None

    def as_dict(self) -> dict:
        return {**asdict(self), "supported": self.supported}


def classify(path: str, *, driver_available=None) -> str:
    low = path.lower()
    if ".gdb/" in low:
        return "filegdb_vector"
    if low.endswith(COMPRESSED_TAR):
        return "compressed_tar"
    if low.endswith(".tar"):
        return "tar_archive"
    if low.endswith((".tif", ".tiff")):
        return "geotiff"
    if low.endswith(".zip"):
        return "zip_archive"
    if low.endswith(".zarr") or ".zarr/" in low:
        return "zarr"
    if low.endswith(".parquet"):
        return "parquet"
    for suffix, driver in DRIVER_FORMATS.items():
        if low.endswith(suffix):
            check = driver_available or _driver_available
            return f"{driver.lower()}_supported" if check(driver) else f"{driver.lower()}_driver_missing"
    if low.startswith("package/") or low.endswith(DOCUMENT_SUFFIXES):
        return "document"
    return "unknown_binary"


def _driver_available(name: str) -> bool:
    from .raster import driver_available

    return driver_available(name)


UNSUPPORTED = {"compressed_tar": "compressed_tar_not_range_readable", "zip_archive": "zip_archive_requires_adapter",
               "zarr": "zarr_requires_adapter", "parquet": "parquet_requires_adapter"}


def is_bundle_listing(paths: list[str]) -> bool:
    """ZIP aktarım paketi: kökte tek arşiv + ``<ad>_ZIP_SHA256.txt`` yan dosyası (içerik ZIP içindedir)."""
    top_zips = [p for p in paths if "/" not in p and p.lower().endswith(".zip")]
    sidecars = [p for p in paths if "/" not in p and p.endswith("_ZIP_SHA256.txt")]
    return len(top_zips) == 1 and len(sidecars) == 1 and not any(p.startswith("PACKAGE/") for p in paths)


def preflight(paths: Iterable[str], *, driver_available=None) -> Preflight:
    paths = list(paths)
    if is_bundle_listing(paths):
        kinds = {p: ("zip_bundle_archive" if p.lower().endswith(".zip") else "document") for p in paths}
        return Preflight(route="zip_bundle", formats=dict(sorted(collections.Counter(kinds.values()).items())),
                         data_files=1)
    kinds = {p: classify(p, driver_available=driver_available) for p in paths}
    counts = Counter(kinds.values())
    result = Preflight(route=None, formats=dict(sorted(counts.items())))
    for p, k in sorted(kinds.items()):
        if k in UNSUPPORTED or k.endswith("_driver_missing") or k.endswith("_supported"):
            # *_supported: sürücü var ama bu biçim için kayıt bağdaştırıcısı henüz yok → yine bağdaştırıcı gerekli.
            result.unsupported.append(p)
    result.data_files = sum(n for k, n in counts.items() if k not in ("document",))
    if result.unsupported:
        kinds_bad = sorted({kinds[p] for p in result.unsupported})
        result.reasons = [UNSUPPORTED.get(k, f"{k}_requires_adapter") for k in kinds_bad]
        return result
    if counts.get("filegdb_vector"):
        if counts.get("tar_archive") or counts.get("geotiff"):
            result.reasons = ["mixed_vector_and_raster_package_requires_adapter"]
            return result
        result.route = "vector_package"
    elif counts.get("tar_archive") or counts.get("geotiff"):
        result.route = "raster_collection"
    elif counts.get("unknown_binary"):
        result.reasons = ["no_recognized_data_format"]
    else:
        result.reasons = ["no_data_files"]
    return result
