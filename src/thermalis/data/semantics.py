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

import hashlib
import json
import os
import re
import threading
import time
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
# Kaynağın veri sözlüğünde beyan edilen yapısal tipler: teknik rol kaynaktan bellidir; bilimsel öznitelik sayılmaz.
STRUCTURAL_TYPES = {
    "OID": "Object ID: row identifier assigned by the File Geodatabase (not a portable or semantic identifier)",
    "Geometry": "Geometry column of the feature class (stored in the layer's native CRS)",
}
# Yalnız ad sözleşmesinden çıkarılan teknik anlamlar (Esri düzenleyici izleme alan adları). Kaynak bunu beyan
# etmediği sürece INFERRED_UNVERIFIED kalır; kullanıcı doğrulamasıyla kesinleşir.
NAME_CONVENTIONS = {
    "CREATED_USER": (("String",), "Name of the user who created the record (Esri editor-tracking naming convention; not declared by this source)"),
    "CREATED_DATE": (("Date", "DateTime"), "Date/time the record was created (Esri editor-tracking naming convention; not declared by this source)"),
    "LAST_EDITED_USER": (("String",), "Name of the user who last edited the record (Esri editor-tracking naming convention; not declared by this source)"),
    "LAST_EDITED_DATE": (("Date", "DateTime"), "Date/time the record was last edited (Esri editor-tracking naming convention; not declared by this source)"),
}
# Metadata kaynak önceliği (yüksekten düşüğe). Her veri türü için genel; yeni türler kendi kaynaklarını ekler.
METADATA_SOURCE_PRIORITY = {
    "domain_labels": ("gdb_field_domain", "package_coded_domains", "package_passport_field_domains"),
    "field_meaning": ("user_verified", "package_passport", "package_data_dictionary", "package_data_dictionary_type", "name_convention"),
    "unit": ("user_verified", "package_declared", "format_rule"),
}
_MOJIBAKE = re.compile(r"Ã[\x80-\xbf]|Ä[\x80-\xbf]|Å[\x80-\xbf]|Ã.|Ä±|Ä°|Åž|ÅŸ|Ã¶|Ã¼|Ã§")


def encoding_issue(text: Any) -> str | None:
    """Metin bozulma belirtisi: U+FFFD, denetim karakteri veya UTF-8'in Latin-1 olarak okunmasından doğan dizi."""
    if not isinstance(text, str):
        return None
    if "�" in text:
        return "replacement_character"
    if any(ord(ch) < 32 and ch not in "\t\n\r" for ch in text):
        return "control_character"
    if _MOJIBAKE.search(text):
        return "mojibake_sequence"
    return None


def domain_label_conflicts(record: dict[str, Any], pp: dict[str, Any] | None, pp_rel: str | None) -> list[dict[str, Any]]:
    """Yetkili domain etiketleri (kayıtta GDB ile birebir doğrulanmış CODED_DOMAINS) ile diğer kaynakları karşılaştırır.

    Yetkili kaynak asla düzeltilmez; ikincil kaynaktaki farklı/bozuk etiket yalnız çelişki olarak kaydedilir.
    Yetkili etiketin kendisinde bozulma belirtisi varsa o da işaretlenir (anlamı tahmin edilmez).
    """
    authoritative = {d.get("name"): {str(k): v for k, v in (d.get("coded_values") or {}).items()} for d in record.get("domains", []) or []}
    out: list[dict[str, Any]] = []
    for name, codes in authoritative.items():
        for code, label in codes.items():
            issue = encoding_issue(label)
            if issue:
                out.append({"kind": "domain_label", "subject": name, "item": code, "authoritative_value": label,
                            "authoritative_source": "PACKAGE/CODED_DOMAINS.json", "other_value": None, "other_source": None,
                            "issue": f"authoritative_{issue}"})
    for fc in ((pp or {}).get("contents") or {}).get("feature_classes", []) or []:
        for field, spec in (fc.get("field_domains") or {}).items():
            if not isinstance(spec, dict):
                continue
            dom, values = spec.get("domain"), spec.get("values") or {}
            auth = authoritative.get(dom)
            for code, label in values.items():
                ref = f"{pp_rel}#contents.feature_classes[{fc.get('layer_name')}].field_domains.{field}"
                if auth is None:
                    issue = "domain_not_in_authoritative_source"
                elif str(code) not in auth:
                    issue = "code_not_in_authoritative_source"
                elif auth[str(code)] != label:
                    issue = f"secondary_{encoding_issue(label)}" if encoding_issue(label) else "label_mismatch"
                else:
                    continue
                out.append({"kind": "domain_label", "subject": dom, "item": str(code), "authoritative_value": (auth or {}).get(str(code)),
                            "authoritative_source": "PACKAGE/CODED_DOMAINS.json", "other_value": label, "other_source": ref, "issue": issue})
    return out


def metadata_conflicts(record: dict[str, Any], pp: dict[str, Any] | None, pp_rel: str | None) -> list[dict[str, Any]]:
    """Tüm metadata çelişkileri: domain etiketleri + bozulma belirtisi taşıyan alan anlamı metinleri (genel)."""
    out = domain_label_conflicts(record, pp, pp_rel)
    for fc in ((pp or {}).get("contents") or {}).get("feature_classes", []) or []:
        for field, text in (fc.get("field_semantics") or {}).items():
            issue = encoding_issue(text)
            if issue:
                out.append({"kind": "field_meaning", "subject": fc.get("layer_name"), "item": field, "authoritative_value": None,
                            "authoritative_source": None, "other_value": text,
                            "other_source": f"{pp_rel}#contents.feature_classes[{fc.get('layer_name')}].field_semantics.{field}",
                            "issue": f"source_{issue}"})
    for row in record.get("data_dictionary", []) or []:
        issue = encoding_issue(row.get("description"))
        if issue:
            out.append({"kind": "field_meaning", "subject": row.get("dataset"), "item": row.get("field"), "authoritative_value": None,
                        "authoritative_source": None, "other_value": row.get("description"),
                        "other_source": f"PACKAGE/DATA_DICTIONARY.csv#{row.get('dataset')}.{row.get('field')}", "issue": f"source_{issue}"})
    return out


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
                "verified_at": None, "verified_by": None, "verification_auth": None, "proposal_compatibility": None,
                "field_role": None,
            }
            if entry["domain_name"]:
                entry["domain_origin"] = ("gdb_field_domain" if f.get("domain") else
                                          "PACKAGE/DATA_DICTIONARY.csv" if d.get("coded_domain") else f"{pp_rel}#field_domains.{name}")
            source_type = d.get("type")
            entry["field_role"] = ("structural" if source_type in STRUCTURAL_TYPES else
                                   "geometry_measure" if name in SHAPE_UNIT_FIELDS else "attribute")
            # Anlam kaynak önceliği (METADATA_SOURCE_PRIORITY["field_meaning"]): pasaport > veri sözlüğü açıklaması
            # > veri sözlüğündeki yapısal tip > ad sözleşmesinden çıkarım (doğrulanmamış).
            text = declared.get(name)
            dict_text = d.get("description") or ""
            # Bozulma belirtisi taşıyan metin kabul edilmez ve onarılmaz: çelişki olarak kaydedilir
            # (metadata_conflicts), anlam bir sonraki temiz kaynaktan gelir ya da UNKNOWN kalır.
            if encoding_issue(text):
                text = None
            if encoding_issue(dict_text):
                dict_text = ""
            if isinstance(text, str) and text.strip():
                entry.update(meaning=text.strip(), status=SOURCE_DECLARED, origin="package_passport",
                             source_ref=f"{pp_rel}#contents.feature_classes[{layer_name}].field_semantics.{name}")
            elif dict_text.strip():
                entry.update(meaning=dict_text.strip(), status=SOURCE_DECLARED, origin="package_data_dictionary",
                             source_ref=f"PACKAGE/DATA_DICTIONARY.csv#{layer_name}.{name}")
            elif source_type in STRUCTURAL_TYPES:
                entry.update(meaning=STRUCTURAL_TYPES[source_type], status=SOURCE_DECLARED, origin=f"package_data_dictionary:type={source_type}",
                             source_ref=f"PACKAGE/DATA_DICTIONARY.csv#{layer_name}.{name}")
            elif name in NAME_CONVENTIONS and (f.get("type") or source_type) in NAME_CONVENTIONS[name][0]:
                entry.update(meaning=NAME_CONVENTIONS[name][1], status=INFERRED_UNVERIFIED, origin="name_convention:esri_editor_tracking",
                             source_ref=None)
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


# --------------------------------------------------------------------------- doğrulama günlüğü
#
# Her satır bir öncekinin özetini taşır (SHA-256 zinciri). Zincir kırılırsa kırılma noktasından sonraki
# satırlar uygulanmaz ve durum raporlanır; satırlar asla sessizce düzeltilmez veya silinmez.
# Doğrulayan adı bir *beyandır* (declared_by); teknik olarak doğrulanan tek şey isteğin yetkili yerel
# kanaldan geldiğidir (auth_method). Çoklu kullanıcıya geçişte auth_method + principal genişletilir.

GENESIS = "0" * 64
LOG_SCHEMA = "thermalis.semantic-verification/2"
_LOG_LOCK = threading.Lock()


def _entry_hash(entry: dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "entry_hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def read_log(data_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(geçerli zincir kayıtları, bütünlük durumu). Bozuk/şemasız satırdan sonrası güvenilmez sayılır."""
    path = verifications_path(data_root)
    if not path.is_file():
        return [], {"status": "empty", "entries": 0, "valid": 0, "broken_at_line": None, "reason": None}
    lines = path.read_text(encoding="utf-8").splitlines()
    valid: list[dict[str, Any]] = []
    prev = GENESIS
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            return valid, {"status": "broken", "entries": len(lines), "valid": len(valid), "broken_at_line": number, "reason": "invalid_json"}
        if not isinstance(item, dict) or item.get("schema") != LOG_SCHEMA:
            return valid, {"status": "broken", "entries": len(lines), "valid": len(valid), "broken_at_line": number, "reason": "unknown_schema"}
        if item.get("prev_hash") != prev or item.get("entry_hash") != _entry_hash(item):
            return valid, {"status": "broken", "entries": len(lines), "valid": len(valid), "broken_at_line": number, "reason": "hash_chain_mismatch"}
        valid.append(item)
        prev = item["entry_hash"]
    return valid, {"status": "ok", "entries": len(valid), "valid": len(valid), "broken_at_line": None, "reason": None, "head": prev}


def load_verifications(data_root: Path) -> list[dict[str, Any]]:
    return read_log(data_root)[0]


class _FileLock:
    """Süreçler arası basit kilit (O_EXCL); aynı süreçteki iş parçacıkları için ayrıca threading.Lock."""

    def __init__(self, path: Path, timeout: float = 10.0) -> None:
        self.path, self.timeout = path, timeout

    def __enter__(self) -> "_FileLock":
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                return self
            except FileExistsError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"verification log locked: {self.path}")
                time.sleep(0.05)

    def __exit__(self, *exc: Any) -> None:
        self.path.unlink(missing_ok=True)


def append_verification(data_root: Path, entry: dict[str, Any], *, auth_method: str = "in_process",
                        content_id: str | None = None, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Doğrulamayı zincirli, yalnız eklenen günlüğe yazar (kilit altında; zincir bozuksa yazmaz)."""
    path = verifications_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_LOCK, _FileLock(path.with_suffix(".lock")):
        valid, integrity = read_log(data_root)
        if integrity["status"] == "broken":
            raise ValueError(f"verification_log_broken:{integrity['broken_at_line']}:{integrity['reason']}")
        record = {
            "schema": LOG_SCHEMA, "seq": len(valid) + 1, "at": _utc_now(),
            "dataset_id": entry.get("dataset_id"), "version": entry.get("version"), "field": entry.get("field"),
            "content_id": content_id, "auth_method": auth_method,
            "declared_by": entry.get("verified_by"), "identity_verified": False, "note": entry.get("note"),
            "values": {k: entry[k] for k in VERIFIABLE if entry.get(k) not in (None, "")},
            "previous": previous or {},
            "prev_hash": valid[-1]["entry_hash"] if valid else GENESIS,
        }
        record["entry_hash"] = _entry_hash(record)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return record


def resolve(entries_by_version: list[tuple[str, str, list[dict[str, Any]]]], verifications: list[dict[str, Any]],
            layer_id_of, content_ids: dict[tuple[str, str], str | None] | None = None) -> list[dict[str, Any]]:
    """Kaynak girdilere kullanıcı doğrulamalarını uygular ve önceki sürümden önerileri taşır.

    ``entries_by_version``: (dataset_id, version, girdiler) — kayıt zamanı sırasıyla; ``version`` dataset sürümüdür.
    ``layer_id_of(dataset_id, layer_name)``: mantıksal veri seti kimliği.
    ``content_ids``: (katman kimliği, dataset sürümü) → içerik kimliği. Doğrulama yalnız kaydedildiği içerik
    kimliği o sürümünkiyle aynıysa uygulanır (yanlış sürüme bağlanmış doğrulama uygulanmaz).
    """
    content_ids = content_ids or {}
    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for v in verifications:
        key = (v.get("dataset_id"), v.get("version"), v.get("field"))
        expected = content_ids.get((key[0], key[1]))
        if expected and v.get("content_id") != expected:
            continue
        merged = {**latest.get(key, {}), **(v.get("values") or {}),
                  "at": v.get("at"), "verified_by": v.get("declared_by"), "auth_method": v.get("auth_method")}
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
                e.update(verified_at=user.get("at"), verified_by=user.get("verified_by"), verification_auth=user.get("auth_method"))
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
