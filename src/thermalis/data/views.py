"""Arayüz için katalog görünümleri: mantıksal veri setleri, tam metadata ve dışa aktarım.

Her değer gerçek kayıtlardan gelir: sorgulanabilir dizin (``catalog_index.sqlite``) ve
sürüm klasöründeki ``version.json`` / ``quality.json`` / ``integrity.json``. Bulunmayan
bilgi ``None`` olarak döner; arayüz bunu "Bilinmiyor / Sağlanmamış" diye gösterir.

Mantıksal veri seti = kayıtlı bir sürümün **yayımlanan (core) katmanı**. Örn. HD01 paketi
iki mantıksal veri seti verir (Districts, Neighborhoods); ikisi aynı paket ve aynı
File Geodatabase varlığına bağlıdır.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

from . import catalog
from .adapters import ADAPTERS
from .catalog_index import _query, finding_summary, query_completeness, query_record_issues, query_versions

ADAPTER_LABELS = {a.key: a.label for a in ADAPTERS}
EXPORT_COLUMNS = (
    "id", "dataset_id", "version", "package_id", "package_version", "layer_name", "title", "category",
    "source_institution", "format", "geometry_type", "feature_count", "crs_name", "extent_minx", "extent_miny",
    "extent_maxx", "extent_maxy", "temporal_state", "temporal_currency_year", "quality_status", "access_class",
    "integrity_status", "integrity_checked_at", "metadata_present", "metadata_total", "registered_at", "description",
)


def _one(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return rows[0] if rows else None


def package_version(package_id: str | None) -> str | None:
    """Paket sürümü paket kimliğinin ``_v<sürüm>`` ekinden okunur; yoksa bilinmiyor."""
    match = re.search(r"_v(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)$", package_id or "")
    return match.group(1) if match else None


def _place(profile: dict[str, Any] | None) -> str | None:
    scope = (profile or {}).get("geographic_scope")
    return scope.split(",")[0].strip() if isinstance(scope, str) and scope.strip() else None


def _integrity(dataset_id: str, version: str, data_root: Path) -> dict[str, Any]:
    rows = _query(data_root, "SELECT * FROM verifications WHERE dataset_id=? AND version=? ORDER BY at", (dataset_id, version))
    last = rows[-1] if rows else None
    last_pass = next((r for r in reversed(rows) if r["status"] == "PASS"), None)
    return {
        "status": last["status"] if last else None,
        "checked_at": last["at"] if last else None,
        "last_successful_at": last_pass["at"] if last_pass else None,
        "history": rows,
    }


def _completeness_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(r["state"] for r in rows)
    return {"total": len(rows), **{k: counts.get(k, 0) for k in ("present", "partial", "missing", "unknown", "not_applicable")}}


def _item(data_root: Path, layer: dict[str, Any], dv: dict[str, Any], profile: dict[str, Any] | None,
          meta: dict[str, Any] | None, version_count: int) -> dict[str, Any]:
    ds, ver = layer["dataset_id"], layer["version"]
    asset = _one(_query(data_root, "SELECT * FROM assets WHERE asset_id=?", (layer["source_asset_id"],))) if layer["source_asset_id"] else None
    completeness = query_completeness(data_root, ds, ver)
    temporal = next((c for c in completeness if c["item"] == "temporal_coverage"), None)
    extent = None if layer["extent_minx"] is None else {
        "minx": layer["extent_minx"], "miny": layer["extent_miny"], "maxx": layer["extent_maxx"], "maxy": layer["extent_maxy"],
        "crs": layer["extent_crs"]}
    return {
        # "version" = mantıksal dataset sürümü; "package_version" = bu satırın ait olduğu paketin sürümü.
        "id": layer["layer_dataset_id"], "dataset_id": ds, "version": layer.get("dataset_version") or ver,
        "package_id": dv["package_id"], "package_version": ver, "content_id": layer.get("content_id"),
        "version_change": layer.get("change"),
        "package_fingerprint": dv["package_fingerprint"],
        "layer_name": layer["layer_name"], "place": _place(profile),
        "source_title": (meta or {}).get("title"),
        "category": (profile or {}).get("category"),
        "source_institution": (meta or {}).get("credits") or (profile or {}).get("providing_organization") or dv["owner"],
        "format": ADAPTER_LABELS.get(asset["format"]) if asset and asset.get("format") else None,
        "format_key": asset["format"] if asset else None,
        "asset_id": layer["source_asset_id"], "asset_path": asset["path"] if asset else None,
        "geometry_type": layer["geometry_type"], "feature_count": layer["feature_count"], "role": layer["role"],
        "crs_name": layer["crs_name"], "spatial_extent": extent,
        "temporal": {"state": temporal["state"] if temporal else "unknown",
                     "currency_year": (profile or {}).get("temporal_currency_year"),
                     "valid_from": (profile or {}).get("valid_from"), "valid_to": (profile or {}).get("valid_to")},
        "quality_status": dv["quality_status"], "access_class": dv["access_class"],
        "integrity": {k: v for k, v in _integrity(ds, ver, data_root).items() if k != "history"},
        "metadata_completeness": _completeness_summary(completeness),
        "registered_at": dv["registered_at"],
        "summary": (meta or {}).get("summary"), "description": (meta or {}).get("description"),
        "record_issue_count": len(query_record_issues(data_root, ds, ver)),
        "version_count": version_count,
        "storage": _storage_summary(data_root, ds, ver),
    }


def _storage_summary(data_root: Path, ds: str, ver: str) -> dict[str, Any]:
    from .catalog_index import storage_location

    loc = storage_location(data_root, ds, ver) or {"backend": "local"}
    copy = _one(_query(data_root, "SELECT bucket, committed_at, same_fingerprint FROM cloud_copies WHERE dataset_id=? AND version=?",
                       (ds, ver)))
    return {"backend": loc["backend"], "provider": loc.get("provider"), "bucket": loc.get("bucket"),
            "committed_at": loc.get("committed_at"), "total_bytes": loc.get("total_bytes"), "synced_at": loc.get("synced_at"),
            # Geçiş döneminde yerel kaydın doğrulanmış bulut kopyası (katalog yerel kaydı kullanmaya devam eder).
            "cloud_copy": None if copy is None else {"bucket": copy["bucket"], "committed_at": copy["committed_at"],
                                                     "same_fingerprint": bool(copy["same_fingerprint"])}}


def catalog_items(data_root: Path) -> list[dict[str, Any]]:
    """Her veri setinin son kayıtlı sürümündeki yayımlanan katmanlar = mantıksal veri setleri."""
    items = []
    for d in _query(data_root, "SELECT * FROM datasets WHERE kind='dataset' ORDER BY dataset_id"):
        versions = query_versions(data_root, d["dataset_id"])
        if not versions:
            continue
        latest = d["latest_registered"] or versions[-1]["version"]
        dv = _one(_query(data_root, "SELECT * FROM dataset_versions WHERE dataset_id=? AND version=?", (d["dataset_id"], latest)))
        if dv is None:
            continue
        profile = _one(_query(data_root, "SELECT * FROM dataset_profile WHERE dataset_id=? AND version=?", (d["dataset_id"], latest)))
        for layer in _query(data_root, "SELECT * FROM layers WHERE dataset_id=? AND version=? AND published=1 ORDER BY layer_name",
                            (d["dataset_id"], latest)):
            meta = _one(_query(data_root, "SELECT * FROM layer_metadata WHERE layer_dataset_id=? AND version=?",
                               (layer["layer_dataset_id"], latest)))
            count = len(_query(data_root, "SELECT 1 FROM layer_versions WHERE layer_dataset_id=?", (layer["layer_dataset_id"],)))
            items.append(_item(data_root, layer, dv, profile, meta, count))
    return items


def dataset_metadata(data_root: Path, layer_id: str, version: str | None = None) -> dict[str, Any] | None:
    """Bir mantıksal veri setinin tam metadata'sı (gerçek kayıtlardan)."""
    from .contract import NotFound, _layer

    try:
        # Dataset sürümü (geriye dönük: paket sürümü de kabul edilir) → sürümün doğduğu paketin katman satırı.
        layer = _layer(data_root, layer_id, version)
    except NotFound:
        try:
            layer = _layer(data_root, layer_id, None)
        except NotFound:
            return None
    ds, ver = layer["dataset_id"], layer["version"]
    from .catalog_index import record_folder

    folder = record_folder(data_root, ds, ver)  # yerel sürüm klasörü veya bulut kaydının doğrulanmış yansısı
    if folder is None or not (folder / "version.json").is_file():
        return None
    from ..storage import read_json

    record, quality, integrity = (read_json(folder / n) for n in ("version.json", "quality.json", "integrity.json"))
    dv = _one(_query(data_root, "SELECT * FROM dataset_versions WHERE dataset_id=? AND version=?", (ds, ver)))
    profile = _one(_query(data_root, "SELECT * FROM dataset_profile WHERE dataset_id=? AND version=?", (ds, ver)))
    meta = _one(_query(data_root, "SELECT * FROM layer_metadata WHERE layer_dataset_id=? AND version=?", (layer_id, ver)))
    from .contract import list_versions

    history = list_versions(data_root, layer_id)  # mantıksal veri setinin kendi sürümleri (paket sürümlerinden bağımsız)
    item = _item(data_root, layer, dv, profile, meta, len(history))

    fields = _query(data_root, "SELECT * FROM fields WHERE layer_dataset_id=? AND version=? ORDER BY rowid", (layer_id, ver))
    used_domains = sorted({f["domain"] for f in fields if f["domain"]})
    domains = []
    for name in used_domains:
        d = _one(_query(data_root, "SELECT * FROM domains WHERE dataset_id=? AND version=? AND name=?", (ds, ver, name)))
        values = _query(data_root, "SELECT code, label FROM domain_values WHERE dataset_id=? AND version=? AND domain=? ORDER BY rowid",
                        (ds, ver, name))
        domains.append({"name": name, "description": (d or {}).get("description"), "values": values})
    layer_names = set(record.get("layers", {}))
    findings = [f for f in _query(data_root, "SELECT * FROM quality_findings WHERE dataset_id=? AND version=? ORDER BY seq", (ds, ver))
                if f["layer_dataset_id"] == layer_id or (f["subject"] not in layer_names)]
    related = [{"id": l["layer_dataset_id"], "layer_name": l["layer_name"], "role": l["role"], "published": bool(l["published"]),
                "feature_count": l["feature_count"], "shares_asset": l["source_asset_id"] == layer["source_asset_id"]}
               for l in _query(data_root, "SELECT * FROM layers WHERE dataset_id=? AND version=? ORDER BY role, layer_name", (ds, ver))
               if l["layer_dataset_id"] != layer_id]
    assets = _query(data_root, "SELECT asset_id, path, kind, format, bytes, sha256, parent_asset_id FROM assets "
                               "WHERE dataset_id=? AND version=? ORDER BY kind, path", (ds, ver))
    integ = _integrity(ds, ver, data_root)
    return {
        "item": item,
        "identity": {"id": layer_id, "dataset_id": ds, "version": layer["dataset_version"], "layer_name": layer["layer_name"],
                     "package_id": dv["package_id"], "package_version": ver, "content_id": layer["content_id"],
                     "content_source": layer["content_source"],
                     "package_fingerprint": dv["package_fingerprint"], "fingerprint_source": dv["fingerprint_source"],
                     "ingest_id": dv["ingest_id"], "registered_at": dv["registered_at"], "tool": dv["tool"], "gdal": dv["gdal"]},
        "version_history": history,
        "source": {**{k: record["source"].get(k) for k in ("owner", "origin_path", "ingested_from", "package_status",
                                                           "source_update_year", "population_scope", "package_built_at_utc")},
                   "profile": profile, "layer_metadata": meta,
                   "copy_verification": record["source"].get("copy_verification")},
        "assets": assets,
        "crs": record.get("crs"),
        "geometry": {"type": layer["geometry_type"], "feature_count": layer["feature_count"],
                     "expected_feature_count": layer["expected_feature_count"], "null_geometry": layer["null_geometry"],
                     "empty_geometry": layer["empty_geometry"], "extent": item["spatial_extent"], "crs_matches_package": layer["crs_matches"]},
        "fields": fields,
        "dictionary": _query(data_root, "SELECT * FROM dictionary WHERE dataset_id=? AND version=? AND layer_name=? ORDER BY rowid",
                             (ds, ver, layer["layer_name"])),
        "domains": domains,
        "quality": {"status": quality.get("status"), "status_source": quality.get("status_source"),
                    "upgrade_policy": quality.get("upgrade_policy"), "steward_disposition": quality.get("steward_disposition"),
                    "fitness_for_use": quality.get("fitness_for_use"),
                    "gates": _query(data_root, "SELECT gate_id, status, detail, source_file FROM quality_gates WHERE dataset_id=? AND version=?", (ds, ver)),
                    "findings": findings, "version_summary": finding_summary(data_root, ds, ver),
                    "layer_status": _query(data_root, "SELECT * FROM layer_status WHERE dataset_id=? AND version=?", (ds, ver))},
        "access": {**record.get("access", {}), "constraints_text": (meta or {}).get("access_constraints")},
        "related_datasets": related,
        "integrity": {"file_count": integrity.get("file_count"), "total_bytes": integrity.get("total_bytes"),
                      "algorithm": integrity.get("algorithm"),
                      "verifications": {k: v.get("status") for k, v in integrity.get("verifications", {}).items()},
                      **integ},
        "record_issues": query_record_issues(data_root, ds, ver),
        "metadata_completeness": query_completeness(data_root, ds, ver),
        "registration_checks": record.get("checks", []),
    }


def export_catalog(data_root: Path, fmt: str = "json") -> tuple[str, str]:
    """(içerik, medya türü). Makine çıktısı: UTF-8, nokta ondalık, dilden bağımsız alan adları."""
    items = catalog_items(data_root)
    if fmt == "json":
        return json.dumps({"schema_version": "1.0", "datasets": items}, ensure_ascii=False, indent=2), "application/json"
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EXPORT_COLUMNS)
    for i in items:
        e = i["spatial_extent"] or {}
        row = {**i, "extent_minx": e.get("minx"), "extent_miny": e.get("miny"), "extent_maxx": e.get("maxx"), "extent_maxy": e.get("maxy"),
               "temporal_state": i["temporal"]["state"], "temporal_currency_year": i["temporal"]["currency_year"],
               "integrity_status": i["integrity"]["status"], "integrity_checked_at": i["integrity"]["checked_at"],
               "metadata_present": i["metadata_completeness"]["present"], "metadata_total": i["metadata_completeness"]["total"],
               "title": " ".join(p for p in (i["place"], i["layer_name"]) if p)}
        writer.writerow(["" if row.get(c) is None else (repr(row[c]) if isinstance(row[c], float) else row[c]) for c in EXPORT_COLUMNS])
    return buffer.getvalue(), "text/csv"
