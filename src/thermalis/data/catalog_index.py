"""DATA kataloğunun sorgulanabilir dizini (SQLite).

Gerçek kaynak sürüm klasörlerindeki ``version.json`` / ``quality.json`` /
``integrity.json`` kayıtlarıdır. Bu dizin onlardan **türetilir**: her yeniden
kurulumda geçici dosyaya baştan yazılır ve ``os.replace`` ile tek adımda yerine
konur. Bu yüzden ikinci bir katalog değildir; silinse bile ``data reindex`` ile
aynen yeniden üretilir. Sürüm kayıtlarına hiçbir zaman yazmaz.

Kimlikler ayrı tutulur:
- Dataset         ``THERMALIS_DATA/HD01_ADMIN_BOUNDARIES/ISTANBUL`` (mantıksal)
- Layer dataset   ``THERMALIS_DATA/HD01_ADMIN_BOUNDARIES/ISTANBUL/DISTRICTS`` (alt katman; bağımsız sorgulanır)
- Dataset version ``(dataset_id, version)``
- Package         ``package_id`` + ``package_fingerprint`` (paket sürümü veri sürümünden ayrıdır)
- Asset           ``ast_<sha256(dataset|version|path)[:20]>`` (fiziksel dosya / kapsayıcı)
- Finding         ``(dataset_id, version, seq)`` — ``exception_id`` tekil olmayabilir (eski kayıtlar)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
from collections import Counter
from contextlib import closing
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from ..text import match_key
from . import catalog
from .integrity import sha256_file

INDEX_FILE = "catalog_index.sqlite"
INDEX_SCHEMA_VERSION = "1.0"
# Kayıt yapısı sorun kodları (frontend: data.meta.issue.<kod> ve data.meta.issue_detail.<kod>; test kontrol eder).
RECORD_ISSUE_CODES = ("DUPLICATE_EXCEPTION_ID", "LAYER_STATUS_COUNTED_AS_EXCEPTION", "FINDING_ORIGIN_NOT_RECORDED",
                      "PACKAGE_FINGERPRINT_NOT_RECORDED", "RECORD_FILES_WRITABLE")
REGISTER_SOURCE_SUFFIX = "EXCEPTION_REGISTER.csv"

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE datasets(
  dataset_id TEXT PRIMARY KEY, kind TEXT NOT NULL, parent_dataset_id TEXT, family TEXT,
  layer_name TEXT, latest_registered TEXT);
CREATE TABLE dataset_versions(
  dataset_id TEXT, version TEXT, title_label TEXT, package_id TEXT, package_type TEXT, package_status TEXT,
  package_built_at_utc TEXT, package_fingerprint TEXT, fingerprint_source TEXT,
  registration_status TEXT, ingest_id TEXT, registered_at TEXT, tool TEXT, gdal TEXT,
  quality_status TEXT, access_class TEXT, public_release INTEGER, authorization TEXT, access_policy TEXT,
  owner TEXT, source_update_year TEXT, origin_path TEXT, ingested_from TEXT, temporal_coverage TEXT,
  crs_name TEXT, crs_wkt TEXT, crs_factory_code TEXT, crs_unit TEXT, crs_transformed INTEGER,
  record_dir TEXT, PRIMARY KEY(dataset_id, version));
CREATE TABLE layers(
  layer_dataset_id TEXT, version TEXT, dataset_id TEXT, layer_name TEXT, role TEXT, published INTEGER,
  geometry_type TEXT, feature_count INTEGER, expected_feature_count INTEGER, crs_name TEXT, crs_matches INTEGER,
  extent_minx REAL, extent_miny REAL, extent_maxx REAL, extent_maxy REAL, extent_crs TEXT,
  null_geometry INTEGER, empty_geometry INTEGER, source_asset_id TEXT,
  PRIMARY KEY(layer_dataset_id, version));
CREATE TABLE fields(
  layer_dataset_id TEXT, version TEXT, name TEXT, type TEXT, alias TEXT, width INTEGER, nullable INTEGER,
  domain TEXT, null_count INTEGER, dictionary_type TEXT, dictionary_alias TEXT,
  PRIMARY KEY(layer_dataset_id, version, name));
CREATE TABLE dictionary(
  dataset_id TEXT, version TEXT, layer_name TEXT, field TEXT, alias TEXT, type TEXT, length TEXT,
  nullable TEXT, required TEXT, coded_domain TEXT, description TEXT,
  PRIMARY KEY(dataset_id, version, layer_name, field));
CREATE TABLE domains(dataset_id TEXT, version TEXT, name TEXT, description TEXT, code_count INTEGER,
  PRIMARY KEY(dataset_id, version, name));
CREATE TABLE domain_values(dataset_id TEXT, version TEXT, domain TEXT, code TEXT, label TEXT,
  PRIMARY KEY(dataset_id, version, domain, code));
CREATE TABLE assets(
  asset_id TEXT PRIMARY KEY, dataset_id TEXT, version TEXT, path TEXT, kind TEXT, format TEXT,
  bytes INTEGER, sha256 TEXT, parent_asset_id TEXT);
CREATE TABLE quality_findings(
  dataset_id TEXT, version TEXT, seq INTEGER, exception_id TEXT, id_unique INTEGER, origin TEXT, kind TEXT,
  category TEXT, subject TEXT, layer_dataset_id TEXT, feature_a TEXT, oid_a INTEGER, feature_b TEXT, oid_b INTEGER,
  area_m2 REAL, percent_of_feature REAL, evidence TEXT, qa_action TEXT, disposition TEXT, source_file TEXT,
  PRIMARY KEY(dataset_id, version, seq));
CREATE TABLE layer_status(dataset_id TEXT, version TEXT, layer_name TEXT, geometry_state TEXT, published INTEGER,
  source_file TEXT, record_location TEXT);
CREATE TABLE quality_gates(dataset_id TEXT, version TEXT, gate_id TEXT, status TEXT, detail TEXT, source_file TEXT,
  PRIMARY KEY(dataset_id, version, gate_id));
CREATE TABLE checks(dataset_id TEXT, version TEXT, seq INTEGER, check_id TEXT, title TEXT, status TEXT, detail TEXT,
  PRIMARY KEY(dataset_id, version, seq));
CREATE TABLE relations(subject TEXT, predicate TEXT, object TEXT, dataset_id TEXT, version TEXT, evidence TEXT);
CREATE TABLE metadata_completeness(dataset_id TEXT, version TEXT, item TEXT, state TEXT, detail TEXT,
  PRIMARY KEY(dataset_id, version, item));
CREATE TABLE record_issues(dataset_id TEXT, version TEXT, code TEXT, detail TEXT, evidence TEXT);
CREATE TABLE layer_metadata(layer_dataset_id TEXT, version TEXT, title TEXT, summary TEXT, description TEXT,
  credits TEXT, tags TEXT, access_constraints TEXT, source_file TEXT, PRIMARY KEY(layer_dataset_id, version));
CREATE TABLE dataset_profile(dataset_id TEXT, version TEXT, title TEXT, title_tr TEXT, category TEXT, themes TEXT,
  geographic_scope TEXT, providing_organization TEXT, origin_system TEXT, temporal_currency_year TEXT,
  valid_from TEXT, valid_to TEXT, temporal_note TEXT, source_file TEXT, PRIMARY KEY(dataset_id, version));
CREATE TABLE verifications(dataset_id TEXT, version TEXT, at TEXT, status TEXT, kind TEXT, detail TEXT);
CREATE TABLE ingest_runs(item_id TEXT PRIMARY KEY, package_name TEXT, dataset_id TEXT, version TEXT,
  technical_status TEXT, quality_status TEXT, audit_status TEXT, access_class TEXT, started_at TEXT,
  finished_at TEXT, result_path TEXT);
CREATE INDEX ix_findings_category ON quality_findings(dataset_id, version, category);
CREATE INDEX ix_layers_dataset ON layers(dataset_id, version);
CREATE INDEX ix_assets_version ON assets(dataset_id, version);
"""


def index_path(data_root: Path) -> Path:
    return catalog.data_dir(data_root) / INDEX_FILE


def layer_dataset_id(dataset_id: str, layer_name: str) -> str:
    """Alt katman kimliği: ``<dataset_id>/<KATMAN>`` (Türkçe güvenli ASCII, büyük harf)."""
    segment = re.sub(r"[^A-Z0-9]+", "_", match_key(layer_name).upper()).strip("_") or "LAYER"
    return f"{dataset_id}/{segment}"


def asset_id(dataset_id: str, version: str, path: str) -> str:
    return "ast_" + hashlib.sha256(f"{dataset_id}|{version}|{path}".encode("utf-8")).hexdigest()[:20]


def _read(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: Any) -> str | None:
    """Kaynak metadata'daki ArcGIS HTML açıklamalarını düz metne çevirir (işaretleme asla yorumlanmaz)."""
    if not isinstance(value, str) or not value.strip():
        return None
    parser = _TextOnly()
    parser.feed(value)
    text = " ".join(" ".join(parser.parts).split())
    return text or None


def _payload_json(folder: Path, rel: str) -> Any:
    """Sürümün payload'ındaki bir belgeyi okur; yoksa/bozuksa ``None`` (bilinmeyen = bilinmeyen)."""
    path = folder / "payload" / Path(*rel.split("/"))
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def dataset_profile(folder: Path, record: dict[str, Any]) -> dict[str, Any] | None:
    """Paketteki ``*_DATASET_PASSPORT.json`` belgesinden kimlik, köken ve zaman bilgisi (varsa)."""
    passport_rel = next((d for d in record.get("documents", []) if d.endswith("_DATASET_PASSPORT.json")), None)
    passport = _payload_json(folder, passport_rel) if passport_rel else None
    if not isinstance(passport, dict):
        return None
    identity = passport.get("identity") or {}
    provenance = passport.get("provenance") or {}
    st = passport.get("spatial_temporal") or {}
    themes = identity.get("theme") if isinstance(identity.get("theme"), list) else []
    return {
        "title": identity.get("dataset_title"), "title_tr": identity.get("dataset_title_tr"),
        "category": themes[0] if themes else None, "themes": themes,
        "geographic_scope": identity.get("geographic_scope"),
        "providing_organization": provenance.get("providing_organization"), "origin_system": provenance.get("origin_system"),
        "temporal_currency_year": None if st.get("temporal_currency_year") is None else str(st.get("temporal_currency_year")),
        "valid_from": st.get("precise_valid_from"), "valid_to": st.get("precise_valid_to"),
        "temporal_note": st.get("decision_date_scope"), "source_file": passport_rel,
    }


def finding_origin(record: dict[str, Any]) -> str:
    if record.get("origin"):
        return str(record["origin"])
    # Eski kayıtlarda (``origin`` alanı yok) köken kaynak dosyasından çıkarılır.
    return "package_exception_register" if str(record.get("source") or "").endswith(REGISTER_SOURCE_SUFFIX) else "ingest_derived"


def finding_kind(category: str) -> str:
    if category == "AUXILIARY_LAYER_STATUS":
        return "layer_status"
    if category == "INTENTIONAL_NULL_IDENTIFIER":
        return "documented_condition"
    return "finding"


def metadata_completeness(record: dict[str, Any], quality: dict[str, Any],
                          profile: dict[str, Any] | None = None) -> list[tuple[str, str, str]]:
    """(öğe, durum, ayrıntı). Durumlar: present | partial | missing | unknown | not_applicable. Hiçbir değer uydurulmaz."""
    src = record.get("source", {})
    access = record.get("access", {})
    crs = record.get("crs", {})
    layers = record.get("layers", {})
    docs = record.get("documents", [])
    dictionary = record.get("data_dictionary", [])
    out: list[tuple[str, str, str]] = []

    def item(name: str, ok: bool | None, detail: str = "") -> None:
        out.append((name, "present" if ok else ("unknown" if ok is None else "missing"), detail))

    item("dataset_id", bool(record.get("dataset_id")), record.get("dataset_id", ""))
    item("version", bool(record.get("version")), record.get("version", ""))
    item("source_owner", bool(src.get("owner")), str(src.get("owner") or ""))
    item("provenance_origin_path", bool(src.get("origin_path")), str(src.get("origin_path") or ""))
    lic = [d for d in docs if "LICENSE" in d.upper()]
    item("license_document", bool(lic), ", ".join(lic))
    cls = access.get("classification")
    item("access_classification", None if cls in (None, "UNKNOWN", "UNSPECIFIED") else True, str(cls))
    item("access_authorization", bool(access.get("authorization")), str(access.get("authorization") or ""))
    item("crs_wkt", bool(crs.get("native_wkt")), str(crs.get("native_crs_name") or ""))
    item("crs_unit", bool(crs.get("unit")), str(crs.get("unit") or ""))
    spatial = [n for n, l in layers.items() if l.get("extent_native")]
    with_geom = [n for n, l in layers.items() if l.get("geometry_type") not in (None, "None")]
    item("spatial_extent", bool(with_geom) and set(with_geom) <= set(spatial), f"{len(spatial)}/{len(with_geom)} katman")
    temporal = src.get("temporal_coverage")
    if isinstance(temporal, str) and temporal.lower() == "not_applicable":
        out.append(("temporal_coverage", "not_applicable", "paket beyanı"))
    elif temporal:
        out.append(("temporal_coverage", "present", json.dumps(temporal, ensure_ascii=False)))
    elif profile and profile.get("temporal_currency_year"):
        both = profile.get("valid_from") and profile.get("valid_to")
        out.append(("temporal_coverage", "present" if both else "partial",
                    f"güncellik yılı {profile['temporal_currency_year']}; geçerlilik başlangıç/bitiş: "
                    f"{profile.get('valid_from') or 'yok'} / {profile.get('valid_to') or 'yok'} ({profile.get('source_file')})"))
    else:
        out.append(("temporal_coverage", "unknown", "pakette beyan yok"))
    item("vector_fields", bool(layers) and all(l.get("fields") for l in layers.values()), f"{len(layers)} katman")
    item("coded_domains", bool(record.get("domains")) or None, f"{len(record.get('domains', []))} domain")
    item("data_dictionary", bool(dictionary), f"{len(dictionary)} satır")
    described = [r for r in dictionary if (r.get("description") or "").strip()]
    out.append(("semantic_definitions", "present" if dictionary and len(described) == len(dictionary) else ("missing" if not described else "partial"),
                f"{len(described)}/{len(dictionary)} alan tanımlı"))
    item("quality_findings", "exceptions" in quality, f"{len(quality.get('exceptions', []))} kayıt")
    item("quality_gates", bool(quality.get("gates")), f"{len(quality.get('gates', []))} kapı")
    return out


def _version_dirs(data_root: Path) -> list[Path]:
    root = catalog.data_dir(data_root) / "datasets"
    return sorted(p.parent for p in root.rglob("version.json")) if root.is_dir() else []


def _registered_fingerprint(folder: Path, record: dict[str, Any]) -> tuple[str | None, str]:
    fp = record.get("source", {}).get("package_fingerprint")
    if fp:
        return fp, "version.json"
    manifest = folder / "payload" / "PACKAGE" / "MANIFEST.json"
    if manifest.is_file():
        return sha256_file(manifest), "payload/PACKAGE/MANIFEST.json (eski kayıt; hesaplandı)"
    return None, "yok"


def registered_fingerprint(folder: Path) -> str | None:
    return _registered_fingerprint(folder, _read(folder / "version.json"))[0]


def _index_version(con: sqlite3.Connection, folder: Path) -> None:
    record = _read(folder / "version.json")
    if record.get("registration", {}).get("status") != "REGISTERED":
        return
    quality = _read(folder / "quality.json")
    integrity = _read(folder / "integrity.json")
    ds, ver = record["dataset_id"], record["version"]
    src, access, crs, reg = record.get("source", {}), record.get("access", {}), record.get("crs", {}), record["registration"]
    fp, fp_source = _registered_fingerprint(folder, record)
    con.execute(
        "INSERT INTO dataset_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ds, ver, (record.get("title") or {}).get("label"), src.get("package_id"), src.get("package_type"),
         src.get("package_status"), src.get("package_built_at_utc"), fp, fp_source,
         reg.get("status"), reg.get("ingest_id"), reg.get("registered_at"), reg.get("tool"), reg.get("gdal"),
         quality.get("status"), "UNKNOWN" if access.get("classification") in (None, "UNSPECIFIED") else access.get("classification"),
         None if access.get("public_release") is None else int(bool(access.get("public_release"))),
         access.get("authorization"), access.get("policy"), src.get("owner"), src.get("source_update_year"),
         src.get("origin_path"), src.get("ingested_from"),
         json.dumps(src.get("temporal_coverage"), ensure_ascii=False) if src.get("temporal_coverage") else None,
         crs.get("native_crs_name"), crs.get("native_wkt"), None if crs.get("factory_code") is None else str(crs.get("factory_code")),
         crs.get("unit"), int(bool(crs.get("transformed"))), str(folder)),
    )
    con.execute("INSERT OR IGNORE INTO datasets VALUES (?,?,?,?,?,?)", (ds, "dataset", None, record.get("family"), None, None))

    # Varlıklar: manifest dosyaları + kapsayıcılar (ör. .gdb).
    containers = {s[len("payload/"):] if s.startswith("payload/") else s for s in record.get("storage", {}).get("vector_sources", [])}
    container_ids = {}
    for c in sorted(containers):
        aid = asset_id(ds, ver, c)
        container_ids[c] = aid
        con.execute("INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?)", (aid, ds, ver, c, "container", "filegdb_vector", None, None, None))
        con.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)", (aid, "part_of_version", f"{ds}@{ver}", ds, ver, "version.json storage.vector_sources"))
    documents = set(record.get("documents", []))
    for f in integrity.get("files", []):
        path = f["path"]
        parent = next((container_ids[c] for c in containers if path.startswith(c + "/")), None)
        kind = "document" if path in documents else ("container_member" if parent else "data_file")
        aid = asset_id(ds, ver, path)
        con.execute("INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?)", (aid, ds, ver, path, kind, None, f["bytes"], f["sha256"], parent))

    for name, layer in record.get("layers", {}).items():
        lid = layer_dataset_id(ds, name)
        ext = layer.get("extent_native") or {}
        source = layer.get("source")
        con.execute("INSERT OR IGNORE INTO datasets VALUES (?,?,?,?,?,?)", (lid, "layer", ds, record.get("family"), name, None))
        con.execute(
            "INSERT INTO layers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (lid, ver, ds, name, layer.get("role"), int(bool(layer.get("published"))), layer.get("geometry_type"),
             layer.get("feature_count"), layer.get("expected_feature_count"), layer.get("crs_name"),
             None if layer.get("crs_matches_package_wkt") is None else int(bool(layer.get("crs_matches_package_wkt"))),
             ext.get("minx"), ext.get("miny"), ext.get("maxx"), ext.get("maxy"), crs.get("native_crs_name") if ext else None,
             layer.get("null_geometry"), layer.get("empty_geometry"), container_ids.get(source)),
        )
        con.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)", (lid, "layer_of", f"{ds}@{ver}", ds, ver, f"version.json layers.{name}"))
        if source in container_ids:
            con.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)", (lid, "stored_in", container_ids[source], ds, ver, source))
        for f in layer.get("fields", []):
            con.execute("INSERT INTO fields VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (lid, ver, f["name"], f.get("type"), f.get("alias"), f.get("width"),
                         None if f.get("nullable") is None else int(bool(f["nullable"])), f.get("domain"),
                         f.get("null_count"), f.get("dictionary_type"), f.get("dictionary_alias")))

    for row in record.get("data_dictionary", []):
        con.execute("INSERT OR REPLACE INTO dictionary VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (ds, ver, row.get("dataset"), row.get("field"), row.get("alias"), row.get("type"), row.get("length"),
                     row.get("nullable"), row.get("required"), row.get("coded_domain") or None, row.get("description") or None))
    for d in record.get("domains", []):
        values = d.get("coded_values", {}) or {}
        con.execute("INSERT INTO domains VALUES (?,?,?,?,?)", (ds, ver, d.get("name"), d.get("description"), len(values)))
        for code, label in values.items():
            con.execute("INSERT INTO domain_values VALUES (?,?,?,?,?)", (ds, ver, d.get("name"), str(code), label))

    exceptions = quality.get("exceptions", [])
    ids = Counter(e.get("exception_id") for e in exceptions)
    layer_names = set(record.get("layers", {}))
    for seq, e in enumerate(exceptions):
        cat = e.get("category") or ""
        subject = e.get("dataset")
        con.execute(
            "INSERT INTO quality_findings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ds, ver, seq, e.get("exception_id"), int(ids[e.get("exception_id")] == 1), finding_origin(e), finding_kind(cat), cat,
             subject, layer_dataset_id(ds, subject) if subject in layer_names else None,
             e.get("feature_a"), e.get("oid_a"), e.get("feature_b"), e.get("oid_b"), e.get("area_m2"),
             e.get("percent_of_feature"), e.get("evidence"), e.get("qa_action"), e.get("disposition"), e.get("source")),
        )
        if e.get("source"):
            con.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)",
                        (f"finding:{ds}@{ver}#{seq}", "evidenced_by", asset_id(ds, ver, e["source"]), ds, ver, e["source"]))
    for s in quality.get("layer_status", []):
        con.execute("INSERT INTO layer_status VALUES (?,?,?,?,?,?,?)",
                    (ds, ver, s.get("layer"), s.get("geometry_state"), int(bool(s.get("published"))), s.get("source"), "quality.json layer_status"))
    for seq, e in enumerate(exceptions):
        if e.get("category") == "AUXILIARY_LAYER_STATUS":  # eski kayıt biçimi
            state = (e.get("evidence") or "").split(";")[0].replace("geometri durumu:", "").strip()
            con.execute("INSERT INTO layer_status VALUES (?,?,?,?,?,?,?)",
                        (ds, ver, e.get("dataset"), state or None, 0, e.get("source"), f"quality.json exceptions[{seq}] (eski biçim)"))
    for g in quality.get("gates", []):
        con.execute("INSERT OR REPLACE INTO quality_gates VALUES (?,?,?,?,?,?)",
                    (ds, ver, g.get("id"), g.get("status"), g.get("detail"), quality.get("gates_source")))
    for seq, c in enumerate(record.get("checks", [])):
        con.execute("INSERT INTO checks VALUES (?,?,?,?,?,?,?)", (ds, ver, seq, c.get("id"), c.get("title"), c.get("status"), c.get("detail")))

    con.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)",
                (f"{ds}@{ver}", "registered_from_package", f"{src.get('package_id')}#{fp}", ds, ver, fp_source))
    for doc in sorted(documents):
        con.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)", (asset_id(ds, ver, doc), "documents", f"{ds}@{ver}", ds, ver, doc))
    for derived in record.get("derived_artifacts", []):
        con.execute("INSERT INTO relations VALUES (?,?,?,?,?,?)",
                    (str(derived.get("id") if isinstance(derived, dict) else derived), "derived_from", f"{ds}@{ver}", ds, ver, "version.json derived_artifacts"))
    profile = dataset_profile(folder, record)
    if profile:
        con.execute("INSERT INTO dataset_profile VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ds, ver, profile["title"], profile["title_tr"], profile["category"], json.dumps(profile["themes"], ensure_ascii=False),
                     profile["geographic_scope"], profile["providing_organization"], profile["origin_system"],
                     profile["temporal_currency_year"], profile["valid_from"], profile["valid_to"], profile["temporal_note"], profile["source_file"]))
    inventory = _payload_json(folder, "PACKAGE/DATASET_INVENTORY_AND_SCHEMA.json")
    for name in record.get("layers", {}):
        meta = (inventory or {}).get(name, {}).get("metadata") if isinstance((inventory or {}).get(name), dict) else None
        if isinstance(meta, dict):
            con.execute("INSERT INTO layer_metadata VALUES (?,?,?,?,?,?,?,?,?)",
                        (layer_dataset_id(ds, name), ver, html_to_text(meta.get("title")), html_to_text(meta.get("summary")),
                         html_to_text(meta.get("description")), html_to_text(meta.get("credits")), html_to_text(meta.get("tags")),
                         html_to_text(meta.get("accessConstraints")), "PACKAGE/DATASET_INVENTORY_AND_SCHEMA.json"))
    final = integrity.get("verifications", {}).get("final_location", {})
    if final:
        con.execute("INSERT INTO verifications VALUES (?,?,?,?,?,?)",
                    (ds, ver, reg.get("registered_at"), final.get("status"), "registration",
                     f"{final.get('checked_files')} dosya SHA-256 (nihai konum)"))
    for item, state, detail in metadata_completeness(record, quality, profile):
        con.execute("INSERT INTO metadata_completeness VALUES (?,?,?,?,?)", (ds, ver, item, state, detail))

    # Kayıt yapısındaki sorunlar (değiştirilmez; yalnız raporlanır).
    issues: list[tuple[str, str, str]] = []
    for eid, n in sorted(ids.items(), key=lambda kv: str(kv[0])):
        if n > 1 or not eid:
            seqs = [i for i, e in enumerate(exceptions) if e.get("exception_id") == eid]
            issues.append(("DUPLICATE_EXCEPTION_ID", f"{eid} × {n}", f"quality.json exceptions{seqs}"))
    legacy_status = [i for i, e in enumerate(exceptions) if e.get("category") == "AUXILIARY_LAYER_STATUS"]
    if legacy_status:
        # Ayrıntılar dilden bağımsız değerdir; cümleyi arayüz seçili dilde kurar (data.meta.issue_detail.<kod>).
        issues.append(("LAYER_STATUS_COUNTED_AS_EXCEPTION", str(len(legacy_status)), f"quality.json exceptions{legacy_status}"))
    if any("origin" not in e for e in exceptions):
        issues.append(("FINDING_ORIGIN_NOT_RECORDED", "", "quality.json exceptions[*].source"))
    if fp_source != "version.json":
        issues.append(("PACKAGE_FINGERPRINT_NOT_RECORDED", "payload/PACKAGE/MANIFEST.json", "version.json source"))
    writable = [n for n in ("version.json", "quality.json", "integrity.json") if os.stat(folder / n).st_mode & stat.S_IWUSR]
    if writable:
        issues.append(("RECORD_FILES_WRITABLE", ", ".join(writable), str(folder)))
    for code, detail, evidence in issues:
        con.execute("INSERT INTO record_issues VALUES (?,?,?,?,?)", (ds, ver, code, detail, evidence))


def _index_runs(con: sqlite3.Connection, data_root: Path) -> None:
    runs = catalog.data_dir(data_root) / "_ingest" / "runs"
    for result in sorted(runs.rglob("INGESTION_RESULT.json")) if runs.is_dir() else []:
        r = _read(result)
        con.execute("INSERT OR REPLACE INTO ingest_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (r.get("item_id"), r.get("package_name"), r.get("dataset_id"), r.get("version"),
                     r["status"]["technical"], r["status"]["quality"], r["status"]["independent_audit"], r["status"]["access"],
                     r.get("started_at"), r.get("finished_at"), str(result)))
    log = catalog.data_dir(data_root) / "_ingest" / "verifications.jsonl"
    for line in log.read_text(encoding="utf-8").splitlines() if log.is_file() else []:
        try:
            v = json.loads(line)
        except ValueError:
            continue
        con.execute("INSERT INTO verifications VALUES (?,?,?,?,?,?)",
                    (v.get("dataset_id"), v.get("version"), v.get("at"), v.get("status"), "reverification", v.get("detail")))


def rebuild_index(data_root: Path) -> Path:
    """Dizini kayıtlardan baştan üretir ve tek adımda yerine koyar."""
    target = index_path(data_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".catalog_index.", suffix=".tmp", dir=target.parent)
    os.close(fd)
    try:
        with closing(sqlite3.connect(tmp)) as con:
            con.executescript(SCHEMA)
            con.execute("INSERT INTO meta VALUES ('schema_version', ?)", (INDEX_SCHEMA_VERSION,))
            for folder in _version_dirs(data_root):
                _index_version(con, folder)
            for dataset_file in sorted((catalog.data_dir(data_root) / "datasets").rglob("dataset.json")) if (catalog.data_dir(data_root) / "datasets").is_dir() else []:
                d = _read(dataset_file)
                con.execute("UPDATE datasets SET latest_registered=? WHERE dataset_id=?", (d.get("latest_registered"), d.get("dataset_id")))
            _index_runs(con, data_root)
            con.commit()
        for attempt in range(20):  # Windows: okuyan bir bağlantı açıkken yer değiştirme kısa süre reddedilebilir
            try:
                os.replace(tmp, target)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.1 * (attempt + 1))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def _query(data_root: Path, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    path = index_path(data_root)
    if not path.is_file():
        rebuild_index(data_root)
    # Salt okunur bağlantı: sorgular dizine yazamaz.
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, params)]


def query_layers(data_root: Path, dataset_id: str | None = None, version: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM layers WHERE (? IS NULL OR dataset_id=? OR layer_dataset_id=?) AND (? IS NULL OR version=?) ORDER BY dataset_id, version, role, layer_name"
    return _query(data_root, sql, (dataset_id, dataset_id, dataset_id, version, version))


def query_fields(data_root: Path, layer_id: str, version: str) -> list[dict[str, Any]]:
    return _query(data_root, "SELECT * FROM fields WHERE layer_dataset_id=? AND version=? ORDER BY rowid", (layer_id, version))


def query_findings(data_root: Path, dataset_id: str, version: str, category: str | None = None,
                   layer_id: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT * FROM quality_findings WHERE dataset_id=? AND version=? AND (? IS NULL OR category=?) "
           "AND (? IS NULL OR layer_dataset_id=?) AND (? IS NULL OR kind=?) ORDER BY seq")
    return _query(data_root, sql, (dataset_id, version, category, category, layer_id, layer_id, kind, kind))


def query_assets(data_root: Path, dataset_id: str, version: str) -> list[dict[str, Any]]:
    return _query(data_root, "SELECT * FROM assets WHERE dataset_id=? AND version=? ORDER BY path", (dataset_id, version))


def query_relations(data_root: Path, dataset_id: str, version: str) -> list[dict[str, Any]]:
    return _query(data_root, "SELECT * FROM relations WHERE dataset_id=? AND version=?", (dataset_id, version))


def query_record_issues(data_root: Path, dataset_id: str | None = None, version: str | None = None) -> list[dict[str, Any]]:
    return _query(data_root, "SELECT * FROM record_issues WHERE (? IS NULL OR dataset_id=?) AND (? IS NULL OR version=?)",
                  (dataset_id, dataset_id, version, version))


def query_completeness(data_root: Path, dataset_id: str, version: str) -> list[dict[str, Any]]:
    return _query(data_root, "SELECT item, state, detail FROM metadata_completeness WHERE dataset_id=? AND version=? ORDER BY rowid",
                  (dataset_id, version))


def finding_summary(data_root: Path, dataset_id: str, version: str) -> dict[str, Any]:
    rows = query_findings(data_root, dataset_id, version)
    return {
        "records": len(rows),
        "unique_exception_ids": len({r["exception_id"] for r in rows}),
        "duplicated_exception_ids": sorted({r["exception_id"] for r in rows if not r["id_unique"]}),
        "by_origin": dict(Counter(r["origin"] for r in rows)),
        "by_kind": dict(Counter(r["kind"] for r in rows)),
        "by_category": dict(Counter(r["category"] for r in rows)),
        "findings_from_package_register": sum(1 for r in rows if r["origin"] == "package_exception_register"),
    }


def query_versions(data_root: Path, dataset_id: str | None = None) -> list[dict[str, Any]]:
    sql = ("SELECT dataset_id, version, package_id, package_fingerprint, quality_status, access_class, registered_at, ingest_id "
           # registered_at saniye çözünürlüklüdür; aynı saniyedeki kayıtlar sürüm adına göre sıralanır (belirlenimci).
           "FROM dataset_versions WHERE (? IS NULL OR dataset_id=?) ORDER BY dataset_id, registered_at, version")
    return _query(data_root, sql, (dataset_id, dataset_id))
