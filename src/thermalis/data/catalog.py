"""DATA kataloğunun disk düzeni ve okuma işlevleri.

    <veri kökü>/data/
      datasets/<SEG>/<SEG>/dataset.json                 mantıksal veri seti
      datasets/<SEG>/<SEG>/<sürüm>/                     DEĞİŞMEZ sürüm klasörü
          payload/                                      paketin bayt-bayt kopyası (salt okunur)
          version.json                                  sürüm kaydı: kaynak, katmanlar, şema, CRS, denetimler
          quality.json                                  kalite durumu ve istisna kataloğu
          integrity.json                                bütünlük manifestosu ve doğrulama sonuçları
          <AİLE>_DATA_INGESTION_REPORT.md               teslim raporu
      _staging/<ingest_id>/                             yarım kayıtlar (katalogda görünmez)
      _failed/<ingest_id>/                              başarısız kayıtlar + rapor (katalogda görünmez)

Bir sürüm klasörü yalnız tüm doğrulamalar geçtikten sonra tek bir yeniden adlandırmayla
``datasets/`` altına taşınır. Var olan bir sürüm asla üzerine yazılmaz.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .transfer_package import DATASET_ID_PATTERN, VERSION_PATTERN

DATA_DIR = "data"


def data_dir(data_root: Path) -> Path:
    return data_root / DATA_DIR


def dataset_segments(dataset_id: str) -> list[str]:
    if not DATASET_ID_PATTERN.match(dataset_id):
        raise ValueError(f"invalid dataset id: {dataset_id}")
    return dataset_id.split("/")[1:]


def dataset_dir(data_root: Path, dataset_id: str) -> Path:
    return data_dir(data_root).joinpath("datasets", *dataset_segments(dataset_id))


def version_dir(data_root: Path, dataset_id: str, version: str) -> Path:
    if not VERSION_PATTERN.match(version):
        raise ValueError(f"invalid version: {version}")
    return dataset_dir(data_root, dataset_id) / version


def _read(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def list_versions(data_root: Path) -> list[dict[str, Any]]:
    """Kayıtlı tüm sürümlerin özet listesi (yalnız tamamlanmış kayıtlar)."""
    root = data_dir(data_root) / "datasets"
    if not root.is_dir():
        return []
    summaries = []
    for record_path in sorted(root.rglob("version.json")):
        record = _read(record_path)
        if record.get("registration", {}).get("status") != "REGISTERED":
            continue
        quality = _read(record_path.parent / "quality.json")
        summaries.append(
            {
                "dataset_id": record["dataset_id"],
                "version": record["version"],
                "title": record.get("title"),
                "quality_status": quality["status"],
                "registered_at": record["registration"]["registered_at"],
                "source_owner": record.get("source", {}).get("owner"),
                "access": record.get("access", {}).get("classification"),
                "crs_name": record.get("crs", {}).get("native_crs_name"),
                "layers": [
                    {"name": n, "role": l["role"], "published": l["published"], "geometry_type": l["geometry_type"], "feature_count": l["feature_count"]}
                    for n, l in record.get("layers", {}).items()
                ],
                "exception_count": len(quality.get("exceptions", [])),
            }
        )
    return summaries


def read_version(data_root: Path, dataset_id: str, version: str) -> dict[str, Any] | None:
    folder = version_dir(data_root, dataset_id, version)
    if not (folder / "version.json").is_file():
        return None
    record = _read(folder / "version.json")
    if record.get("registration", {}).get("status") != "REGISTERED":
        return None
    return {
        "record": record,
        "quality": _read(folder / "quality.json"),
        "integrity": _read(folder / "integrity.json"),
    }
