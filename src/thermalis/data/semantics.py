"""Bilimsel anlamsal veri sözlüğü (Semantic Dictionary).

Her alan için anlam, birim, domain ilişkisi ve boş değer anlamı veri seti **ve sürüm** bağlamında
tutulur. Kaynak kayıtlar (sürüm klasörü) değişmez; bu modül onlardan *okur*:

- ``SOURCE_DECLARED``      anlam kaynakta açıkça yazılı (paket pasaportu ``field_semantics``,
                           veri sözlüğü açıklaması, domain tanımı, QA kanıt dosyası)
- ``INFERRED_UNVERIFIED``  sistem önerdi ama doğrulanmadı (ör. biçim kuralından birim; önceki
                           sürümde doğrulanmış tanımın yeni sürüme önerisi)
- ``USER_VERIFIED``        yetkili kullanıcı doğruladı (kalıcı günlük: ``_semantic/verifications.jsonl``)
- ``UNKNOWN``              anlam bilinmiyor; tip bilgisi anlam sayılmaz

Kullanıcı doğrulamaları yalnız eklenen bir günlükte tutulur; dizin her kurulduğunda yeniden
uygulanır, böylece yeniden başlatmada kaybolmaz. Önceki sürümde doğrulanmış tanım yeni sürüme
otomatik kesinleşmez: tip ve domain uyumluluğu işaretlenmiş bir **öneri** olarak taşınır.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import catalog

SOURCE_DECLARED, INFERRED_UNVERIFIED, USER_VERIFIED, UNKNOWN = "SOURCE_DECLARED", "INFERRED_UNVERIFIED", "USER_VERIFIED", "UNKNOWN"
STATUSES = (SOURCE_DECLARED, INFERRED_UNVERIFIED, USER_VERIFIED, UNKNOWN)
# Kullanıcının doğrulayabildiği anlamsal öznitelikler (diğerleri kaynaktan gelir, elle değiştirilmez).
VERIFIABLE = ("meaning", "description", "unit", "unit_system", "null_meaning", "valid_min", "valid_max", "scale", "offset")
# Esri File Geodatabase sistem alanları: değerleri katman CRS'sinin doğrusal biriminde saklanır (biçim kuralı).
SHAPE_UNIT_FIELDS = {"Shape_Length": 1, "Shape_Area": 2}
UNIT_SYMBOLS = {"metre": "m", "meter": "m", "metres": "m", "meters": "m", "m": "m"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def verifications_path(data_root: Path) -> Path:
    return catalog.data_dir(data_root) / "_semantic" / "verifications.jsonl"


def _read_payload_json(folder: Path, rel: str | None) -> Any:
    if not rel:
        return None
    try:
        with (folder / "payload" / Path(*rel.split("/"))).open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def passport(folder: Path, record: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    rel = next((d for d in record.get("documents", []) if d.endswith("_DATASET_PASSPORT.json")), None)
    data = _read_payload_json(folder, rel)
    return (data if isinstance(data, dict) else None), rel


def passport_layer(pp: dict[str, Any] | None, layer: str) -> dict[str, Any]:
    for fc in ((pp or {}).get("contents") or {}).get("feature_classes", []) or []:
        if isinstance(fc, dict) and fc.get("layer_name") == layer:
            return fc
    return {}


def join_relationships(pp: dict[str, Any] | None) -> list[dict[str, Any]]:
    joins = ((pp or {}).get("contents") or {}).get("join_relationships") or []
    return [j for j in joins if isinstance(j, dict) and j.get("from") and j.get("to")]


def source_entries(folder: Path, record: dict[str, Any], quality: dict[str, Any]) -> list[dict[str, Any]]:
    """Bir sürümün kaynakta bulunan anlamsal bilgisi. Uydurma yok: bulunmayan değer ``None`` kalır."""
    pp, pp_rel = passport(folder, record)
    crs_unit = UNIT_SYMBOLS.get(str((record.get("crs") or {}).get("unit") or "").lower())
    dictionary = record.get("data_dictionary", []) or []
    # Kasıtlı boş kimlik kayıtları QA'da belgelidir; kanıt metni alanı adlandırır ("KIMLIKNO NULL (...)").
    null_ids: dict[tuple[str, str], dict[str, Any]] = {}
    for e in quality.get("exceptions", []) or []:
        if e.get("category") == "INTENTIONAL_NULL_IDENTIFIER":
            field = str(e.get("evidence") or "").split(" ", 1)[0]
            if field:
                null_ids.setdefault((e.get("dataset"), field), {"count": 0, "source": e.get("source")})["count"] += 1
    out: list[dict[str, Any]] = []
    for layer_name, layer in (record.get("layers") or {}).items():
        fc = passport_layer(pp, layer_name)
        declared = fc.get("field_semantics") if isinstance(fc.get("field_semantics"), dict) else {}
        declared_domains = fc.get("field_domains") if isinstance(fc.get("field_domains"), dict) else {}
        dict_rows = {r.get("field"): r for r in dictionary if r.get("dataset") == layer_name}
        gdal_fields = {f["name"]: f for f in layer.get("fields", [])}
        names = list(dict.fromkeys([*dict_rows, *gdal_fields]))
        for name in names:
            f = gdal_fields.get(name, {})
            d = dict_rows.get(name, {})
            entry: dict[str, Any] = {
                "layer_name": layer_name, "field": name,
                "data_type": f.get("type") or d.get("type"), "source_type": d.get("type") or None,
                "alias": d.get("alias") or f.get("alias") or None,
                "meaning": None, "description": None, "status": UNKNOWN, "origin": None, "source_ref": None,
                "unit": None, "unit_system": None, "unit_status": None, "unit_origin": None,
                "domain_name": f.get("domain") or (d.get("coded_domain") or None) or (declared_domains.get(name) or {}).get("domain"),
                "domain_origin": None,
                "null_meaning": None, "null_meaning_status": None, "null_meaning_ref": None,
                "null_count": f.get("null_count"),
                "valid_min": None, "valid_max": None, "scale": None, "offset": None,
                "verified_at": None, "verified_by": None, "proposal_compatibility": None,
            }
            if entry["domain_name"]:
                entry["domain_origin"] = ("gdb_field_domain" if f.get("domain") else
                                          "PACKAGE/DATA_DICTIONARY.csv" if d.get("coded_domain") else f"{pp_rel}#field_domains.{name}")
            text = declared.get(name)
            if isinstance(text, str) and text.strip():
                entry.update(meaning=text.strip(), status=SOURCE_DECLARED, origin="package_passport",
                             source_ref=f"{pp_rel}#contents.feature_classes[{layer_name}].field_semantics.{name}")
            elif (d.get("description") or "").strip():
                entry.update(meaning=d["description"].strip(), status=SOURCE_DECLARED, origin="package_data_dictionary",
                             source_ref=f"PACKAGE/DATA_DICTIONARY.csv#{layer_name}.{name}")
            if name in SHAPE_UNIT_FIELDS and crs_unit:
                power = SHAPE_UNIT_FIELDS[name]
                entry.update(unit=crs_unit + ("²" if power == 2 else ""), unit_system="SI", unit_status=INFERRED_UNVERIFIED,
                             unit_origin="format_rule:esri_filegdb_shape_fields_in_crs_linear_unit+crs_unit")
            info = null_ids.get((layer_name, name))
            if info:
                # Dilden bağımsız kod; açıklaması QA kayıtlarında (INTENTIONAL_NULL_IDENTIFIER) ve kanıt dosyasında.
                entry.update(null_meaning=f"DOCUMENTED_SPECIAL_RECORDS:{info['count']}", null_meaning_status=SOURCE_DECLARED,
                             null_meaning_ref=info["source"])
            out.append(entry)
    return out


def load_verifications(data_root: Path) -> list[dict[str, Any]]:
    path = verifications_path(data_root)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def append_verification(data_root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    """Kullanıcı doğrulamasını kalıcı, yalnız eklenen günlüğe yazar."""
    record = {"at": _utc_now(), **{k: entry.get(k) for k in ("dataset_id", "version", "field", "verified_by", "note")},
              **{k: entry[k] for k in VERIFIABLE if k in entry}}
    path = verifications_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def resolve(entries_by_version: list[tuple[str, str, list[dict[str, Any]]]], verifications: list[dict[str, Any]],
            layer_id_of) -> list[dict[str, Any]]:
    """Kaynak girdilere kullanıcı doğrulamalarını uygular ve önceki sürümden önerileri taşır.

    ``entries_by_version``: (dataset_id, version, girdiler) — kayıt zamanı sırasıyla.
    ``layer_id_of(dataset_id, layer_name)``: mantıksal veri seti kimliği.
    """
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for v in verifications:
        key = (v.get("dataset_id"), v.get("version"), v.get("field"))
        merged = {**latest.get(key, {}), **{k: v[k] for k in VERIFIABLE if k in v},
                  "at": v.get("at"), "verified_by": v.get("verified_by")}
        latest[key] = merged
    verified_history: dict[tuple[str, str], dict[str, Any]] = {}  # (layer_id, field) -> son doğrulanmış girdi
    resolved: list[dict[str, Any]] = []
    for dataset_id, version, entries in entries_by_version:
        for src in entries:
            e = dict(src)
            layer_id = layer_id_of(dataset_id, e["layer_name"])
            e["layer_dataset_id"], e["dataset_id"], e["version"] = layer_id, dataset_id, version
            user = latest.get((layer_id, version, e["field"]))
            if user:
                for k in VERIFIABLE:
                    if k in user and user[k] not in (None, ""):
                        e[k] = user[k]
                if "meaning" in user and user["meaning"]:
                    e.update(status=USER_VERIFIED, origin="user", source_ref=verifications_path_hint())
                if "unit" in user and user["unit"]:
                    e.update(unit_status=USER_VERIFIED, unit_origin="user")
                if "null_meaning" in user and user["null_meaning"]:
                    e.update(null_meaning_status=USER_VERIFIED)
                e.update(verified_at=user.get("at"), verified_by=user.get("verified_by"))
            prev = verified_history.get((layer_id, e["field"]))
            if e["status"] == UNKNOWN and prev:
                compatible = prev["data_type"] == e["data_type"] and prev.get("domain_name") == e.get("domain_name")
                e.update(meaning=prev["meaning"], status=INFERRED_UNVERIFIED, origin=f"carried_from:{prev['version']}",
                         proposal_compatibility="compatible" if compatible else "changed",
                         source_ref=f"{layer_id}@{prev['version']}#{e['field']}")
            if e["status"] == USER_VERIFIED:
                verified_history[(layer_id, e["field"])] = e
            resolved.append(e)
    return resolved


def verifications_path_hint() -> str:
    return "_semantic/verifications.jsonl"
