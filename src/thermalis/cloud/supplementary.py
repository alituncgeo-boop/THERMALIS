"""Yayına eklenen ek kayıtlar (genel; her veri seti için aynı kural).

Bulut yansısında veri dosyaları bulunmadığı için, yerelde veri dosyasından OKUNARAK elde edilen bilimsel
bilgiler yayınla birlikte, değişmez kayıt olarak saklanır:

- ``layer_facts.json``          katman içerik kimlikleri (lc1-sha256-multiset). Sürüm kaydında yoksa (eski kayıt)
                                yerel GDB'den YENİDEN hesaplanır ve varsa türetilmiş önbellekle karşılaştırılır.
- ``quality_identifiers.json``  belgelenmiş özel kayıtların kalıcı kimlikleri (GLOBALID, TANITIMKODU).

İçerikler belirlenimcidir (zaman damgası yok): aynı kayıt için her çalıştırmada aynı baytlar üretilir; böylece
kesilen yayın aynı işlem kimliğiyle güvenle sürdürülür.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FACTS_SCHEMA = "thermalis.layer-facts/1"


class SupplementaryError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def canonical(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def layer_facts_record(data_root: Path, folder: Path, record: dict[str, Any]) -> dict[str, Any]:
    """İçerik kimlikleri. Önce sürüm kaydı; yoksa yerel GDB'den hesaplanır ve türetilmiş önbellekle doğrulanır."""
    from ..data import catalog
    from ..data.catalog_index import FACTS_CACHE
    from ..data.vector_inspect import layer_facts
    from ..storage import read_json

    layers = record.get("layers", {}) or {}
    if layers and all(isinstance(l.get("content"), dict) for l in layers.values()):
        facts, source = {n: l["content"] for n, l in layers.items()}, "version.json"
    else:
        facts = {}
        for name, layer in sorted(layers.items()):
            src = layer.get("source")
            path = folder / "payload" / src if src else None
            if path is None or not path.exists():
                raise SupplementaryError("content_source_missing", name)
            facts[name] = layer_facts(path, name)
        source = "computed_from_local_payload"
        cache_path = catalog.data_dir(data_root) / FACTS_CACHE
        if cache_path.is_file():
            try:
                cached = read_json(cache_path).get(folder.relative_to(catalog.data_dir(data_root)).as_posix(), {}).get("layers", {})
            except (OSError, ValueError):
                cached = {}
            for name, value in cached.items():
                if name in facts and value.get("content_id") != facts[name].get("content_id"):
                    raise SupplementaryError("content_id_mismatch_with_derived_cache", name)
    return {"schema": FACTS_SCHEMA, "dataset_id": record.get("dataset_id"), "version": record.get("version"),
            "source": source, "layers": facts}


def build_supplementary(data_root: Path, folder: Path, record: dict[str, Any]) -> dict[str, bytes]:
    """Yayına eklenecek ek kayıtlar (isim → bayt). ``data_root`` dizini bu kaydı içermelidir."""
    from ..data.catalog_index import SUPPLEMENTARY_FACTS, SUPPLEMENTARY_IDENTIFIERS
    from ..data.contract import quality_identifiers

    out = {SUPPLEMENTARY_FACTS: canonical(layer_facts_record(data_root, folder, record))}
    identifiers = quality_identifiers(data_root, record["dataset_id"], record["version"])
    if identifiers["layers"]:
        out[SUPPLEMENTARY_IDENTIFIERS] = canonical(identifiers)
    return out
