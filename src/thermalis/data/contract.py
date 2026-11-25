"""THERMALIS DATA — bilimsel veri erişim sözleşmesi (Data Access Contract, sürüm 1).

Diğer THERMALIS bileşenleri (ANALYSIS LAB, RISK ENGINE, 3D CITY, INTELLIGENCE) veri setlerini
bu sözleşme üzerinden bulur. Aynı işlevler süreç içinden Python ile, süreç dışından
``/api/data/v1/...`` HTTP uçlarıyla kullanılır (ikinci bir API yoktur; HTTP uçları bu
fonksiyonları çağırır).

Kimlikler:
- ``dataset_id``  kalıcı mantıksal veri seti (ör. ``THERMALIS_DATA/HD01_ADMIN_BOUNDARIES/ISTANBUL/DISTRICTS``)
- ``version``     değişmez sürüm (ör. ``1.0.0-rc1``); ``version_id`` = ``<dataset_id>@<version>``
- ``asset_id``    fiziksel varlık (ör. ``.gdb`` kapsayıcısı)

Bir verinin bulunması, belirli bir analize uygun olduğu anlamına gelmez; uygunluk kararını
ilgili analiz metodolojisi verir. Bu modül analiz–veri eşleştirmesi üretmez.

Erişim: ``RESTRICTED`` veri setlerinde fiziksel dosya yolu ve köken yolları yalnız yetkili yerel
bileşene (süreç içi çağrıda ``authorized=True`` veya HTTP'de bileşen belirteci) verilir.
Yolun verilmesi dosyanın dağıtılabileceği anlamına gelmez; erişim koşulları her yanıtta yer alır.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from . import catalog, semantics
from .adapters import ADAPTERS
from .catalog_index import _query, finding_summary, query_completeness, query_record_issues

CONTRACT = "thermalis.data.contract/1"
ADAPTER_LABELS = {a.key: a.label for a in ADAPTERS}
READERS = {"filegdb_vector": {"driver": "OpenFileGDB", "open_mode": "read_only"}}
USAGE_NOTE = ("Bu veri setinin katalogda bulunması belirli bir bilimsel analize uygunluk onayı değildir; "
              "uygunluk kararını ilgili analiz metodolojisi verir.")


class NotFound(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def version_id(dataset_id: str, version: str) -> str:
    return f"{dataset_id}@{version}"


def _one(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return rows[0] if rows else None


def _layer(data_root: Path, dataset_id: str, version: str | None) -> dict[str, Any]:
    """Mantıksal veri seti sürümünü çözer.

    ``version`` önce **dataset sürümü** olarak aranır. Bulunamazsa geriye dönük uyumluluk için
    **paket sürümü** olarak yorumlanır ve o paketteki dataset sürümüne çözülür (``resolved_from``
    alanında açıkça belirtilir). Dönen kayıt sürümün doğduğu paketin katman satırıdır:
    ``version`` = köken paket sürümü (pakete bağlı tablolar için), ``dataset_version`` = dataset sürümü.
    """
    lv = _query(data_root, "SELECT * FROM layer_versions WHERE layer_dataset_id=? ORDER BY ordinal", (dataset_id,))
    if not lv:
        raise NotFound("dataset_not_found")
    resolved_from = None
    if version:
        row = next((r for r in lv if r["dataset_version"] == version), None)
        if row is None:
            pl = _one(_query(data_root, "SELECT * FROM package_layers WHERE layer_dataset_id=? AND package_version=?", (dataset_id, version)))
            if pl is None:
                raise NotFound("version_not_found")
            row = next(r for r in lv if r["dataset_version"] == pl["dataset_version"])
            resolved_from = {"package_version": version, "change": pl["change"]}
    else:
        row = lv[-1]
    layer = dict(_one(_query(data_root, "SELECT * FROM layers WHERE layer_dataset_id=? AND version=?", (dataset_id, row["origin_package_version"]))))
    present = _query(data_root, "SELECT package_dataset_id, package_version, change FROM package_layers WHERE layer_dataset_id=? AND dataset_version=? "
                                "ORDER BY package_version", (dataset_id, row["dataset_version"]))
    layer.update(dataset_version=row["dataset_version"], content_id=row["content_id"], content_algorithm=row["content_algorithm"],
                 content_source=row["content_source"], same_content_as=row["same_content_as"], present_in_packages=present,
                 resolved_from=resolved_from)
    return layer


def _links(dataset_id: str, version: str) -> dict[str, str]:
    q = f"id={dataset_id}&version={version}"
    return {k: f"/api/data/v1/{k}?{q}" for k in ("descriptor", "semantics", "qa", "lineage", "access")} | {
        "versions": f"/api/data/v1/versions?id={dataset_id}", "domain": f"/api/data/v1/domain?{q}&name={{domain}}",
        "field": f"/api/data/v1/field?{q}&name={{field}}", "diff": f"/api/data/v1/diff?id={dataset_id}&from={{version}}&to={version}"}


# --------------------------------------------------------------------------- keşif

def list_datasets(data_root: Path) -> list[dict[str, Any]]:
    out = []
    for d in _query(data_root, "SELECT * FROM datasets WHERE kind='layer' ORDER BY dataset_id"):
        versions = _query(data_root, "SELECT dataset_version FROM layer_versions WHERE layer_dataset_id=? ORDER BY ordinal",
                          (d["dataset_id"],))
        if not versions:
            continue
        latest = _layer(data_root, d["dataset_id"], None)
        role = _one(_query(data_root, "SELECT declared_role FROM layer_profile WHERE layer_dataset_id=? AND version=?",
                           (d["dataset_id"], latest["version"])))
        out.append({"dataset_id": d["dataset_id"], "kind": "layer", "layer_name": d["layer_name"],
                    "package_dataset_id": d["parent_dataset_id"], "catalog_role": latest["role"], "published": bool(latest["published"]),
                    "declared_role": (role or {}).get("declared_role"), "versions": [v["dataset_version"] for v in versions],
                    "latest_version": latest["dataset_version"], "links": _links(d["dataset_id"], latest["dataset_version"])})
    return out


def list_versions(data_root: Path, dataset_id: str) -> list[dict[str, Any]]:
    """Mantıksal veri setinin dataset sürümleri; her biri doğduğu paketi ve içinde bulunduğu paketleri gösterir."""
    rows = _query(data_root, "SELECT * FROM layer_versions WHERE layer_dataset_id=? ORDER BY ordinal", (dataset_id,))
    if not rows:
        raise NotFound("dataset_not_found")
    out = []
    for r in rows:
        layer = _one(_query(data_root, "SELECT feature_count FROM layers WHERE layer_dataset_id=? AND version=?", (dataset_id, r["origin_package_version"])))
        pkg = _one(_query(data_root, "SELECT package_id, package_fingerprint, quality_status, access_class FROM dataset_versions WHERE dataset_id=? AND version=?",
                          (r["package_dataset_id"], r["origin_package_version"])))
        present = _query(data_root, "SELECT package_version, change FROM package_layers WHERE layer_dataset_id=? AND dataset_version=? ORDER BY package_version",
                         (dataset_id, r["dataset_version"]))
        out.append({"version_id": version_id(dataset_id, r["dataset_version"]), "version": r["dataset_version"],
                    "dataset_version": r["dataset_version"], "content_id": r["content_id"], "content_algorithm": r["content_algorithm"],
                    "same_content_as": r["same_content_as"], "origin_package_dataset_id": r["package_dataset_id"],
                    "origin_package_version": r["origin_package_version"], "present_in_packages": present,
                    "feature_count": (layer or {}).get("feature_count"), "registered_at": r["registered_at"], **(pkg or {}), "immutable": True})
    return out


# --------------------------------------------------------------------------- tanımlayıcı

def _physical(data_root: Path, layer: dict[str, Any]) -> tuple[dict[str, Any] | None, Path]:
    from .catalog_index import record_folder

    asset = _one(_query(data_root, "SELECT * FROM assets WHERE asset_id=?", (layer["source_asset_id"],))) if layer["source_asset_id"] else None
    folder = record_folder(data_root, layer["dataset_id"], layer["version"]) or catalog.version_dir(data_root, layer["dataset_id"], layer["version"])
    return asset, folder


def storage_block(data_root: Path, dataset_id: str, version: str, asset: dict[str, Any] | None, allowed: bool) -> dict[str, Any]:
    """Depolama konumu: yerel mi, bulutta mı. Konum ayrıntısı yalnız erişim izni varsa verilir."""
    from .catalog_index import storage_location

    loc = storage_location(data_root, dataset_id, version) or {"backend": "local"}
    out: dict[str, Any] = {"backend": loc["backend"], "provider": loc.get("provider"),
                           "analysis_ready": True, "committed_at": loc.get("committed_at") if loc["backend"] == "r2" else None}
    if allowed and loc["backend"] == "r2" and asset:
        key = f"{loc.get('namespace') or ''}{loc['prefix']}payload/{asset['path']}"
        out.update(bucket=loc.get("bucket"), object_key_prefix=key, gdal_path=f"/vsis3/{loc.get('bucket')}/{key}",
                   access_note="Bulut nesnesi: okumak için yetkili R2 kimlik bilgisi veya önbellek üzerinden malzemeleştirme gerekir.")
    return out


def _access_conditions(dv: dict[str, Any], meta: dict[str, Any] | None, authorized: bool) -> dict[str, Any]:
    cls = dv["access_class"]
    allowed = authorized or cls == "PUBLIC"
    return {"classification": cls, "public_release": None if dv["public_release"] is None else bool(dv["public_release"]),
            "authorization": dv["authorization"], "policy": dv["access_policy"],
            "source_constraints": (meta or {}).get("access_constraints"),
            "physical_access": "available" if allowed else "local_component_token_required",
            "redistribution": "not_permitted" if cls == "RESTRICTED" else ("per_source_license" if cls == "PUBLIC" else "unknown")}


def descriptor(data_root: Path, dataset_id: str, version: str | None = None, *, authorized: bool = False) -> dict[str, Any]:
    layer = _layer(data_root, dataset_id, version)
    ds, ver = layer["dataset_id"], layer["version"]
    dv = _one(_query(data_root, "SELECT * FROM dataset_versions WHERE dataset_id=? AND version=?", (ds, ver)))
    profile = _one(_query(data_root, "SELECT * FROM dataset_profile WHERE dataset_id=? AND version=?", (ds, ver)))
    meta = _one(_query(data_root, "SELECT * FROM layer_metadata WHERE layer_dataset_id=? AND version=?", (dataset_id, ver)))
    role = _one(_query(data_root, "SELECT * FROM layer_profile WHERE layer_dataset_id=? AND version=?", (dataset_id, ver)))
    asset, _ = _physical(data_root, layer)
    fields = _query(data_root, "SELECT name, type, alias, nullable, domain, null_count FROM fields WHERE layer_dataset_id=? AND version=? ORDER BY rowid",
                    (dataset_id, ver))
    verifs = _query(data_root, "SELECT at, status, kind FROM verifications WHERE dataset_id=? AND version=? ORDER BY at", (ds, ver))
    allowed = authorized or dv["access_class"] == "PUBLIC"
    siblings = _query(data_root, "SELECT layer_dataset_id, layer_name, role, published, source_asset_id FROM layers "
                                 "WHERE dataset_id=? AND version=? AND layer_dataset_id<>? ORDER BY layer_name", (ds, ver, dataset_id))
    joins = _query(data_root, "SELECT * FROM field_relations WHERE dataset_id=? AND version=? AND (from_layer_dataset_id=? OR to_layer_dataset_id=?)",
                   (ds, ver, dataset_id, dataset_id))
    fmt = asset["format"] if asset else None
    dsv = layer["dataset_version"]
    return {
        "contract": CONTRACT, "dataset_id": dataset_id, "version": dsv, "version_id": version_id(dataset_id, dsv),
        "dataset_version": dsv, "resolved_from": layer["resolved_from"],
        "content": {"content_id": layer["content_id"], "algorithm": layer["content_algorithm"], "source": layer["content_source"],
                    "same_content_as": layer["same_content_as"]},
        "package_dataset_id": ds,
        "package": {"package_id": dv["package_id"], "package_version": ver, "fingerprint_sha256": dv["package_fingerprint"],
                    "fingerprint_source": dv["fingerprint_source"], "role": "origin_of_this_dataset_version"},
        "present_in_packages": layer["present_in_packages"],
        "storage": storage_block(data_root, ds, ver, asset, allowed),
        "asset": {"asset_id": layer["source_asset_id"], "kind": (asset or {}).get("kind"), "format": fmt,
                  "format_label": ADAPTER_LABELS.get(fmt) if fmt else None, "relative_path": f"payload/{asset['path']}" if asset else None,
                  "layer": layer["layer_name"], "reader": READERS.get(fmt)},
        "layer": {"name": layer["layer_name"], "catalog_role": layer["role"], "published": bool(layer["published"]),
                  "declared_role": (role or {}).get("declared_role"), "declared_role_source": (role or {}).get("source_ref"),
                  "geometry_type": layer["geometry_type"], "feature_count": layer["feature_count"],
                  "expected_feature_count": layer["expected_feature_count"]},
        "crs": {"name": dv["crs_name"], "wkt": dv["crs_wkt"], "epsg": None, "epsg_assumed": False,
                "factory_code": dv["crs_factory_code"], "unit": dv["crs_unit"], "transformed": bool(dv["crs_transformed"]),
                "matches_package_wkt": None if layer["crs_matches"] is None else bool(layer["crs_matches"])},
        "spatial_extent": None if layer["extent_minx"] is None else {
            "minx": layer["extent_minx"], "miny": layer["extent_miny"], "maxx": layer["extent_maxx"], "maxy": layer["extent_maxy"],
            "crs": layer["extent_crs"], "reprojected": False},
        "temporal": {"currency_year": (profile or {}).get("temporal_currency_year"), "valid_from": (profile or {}).get("valid_from"),
                     "valid_to": (profile or {}).get("valid_to"), "note": (profile or {}).get("temporal_note"),
                     "source": (profile or {}).get("source_file")},
        "schema": [{**f, "nullable": None if f["nullable"] is None else bool(f["nullable"])} for f in fields],
        "quality": {"status": dv["quality_status"], "summary": finding_summary(data_root, ds, ver), "links": {"qa": _links(dataset_id, dsv)["qa"]}},
        "access": _access_conditions(dv, meta, authorized),
        "integrity": {"algorithm": "SHA-256", "status": verifs[-1]["status"] if verifs else None,
                      "last_successful_at": next((v["at"] for v in reversed(verifs) if v["status"] == "PASS"), None), "history": verifs},
        "provenance": {"owner": dv["owner"], "source_institution": (meta or {}).get("credits") or (profile or {}).get("providing_organization"),
                       "origin_system": (profile or {}).get("origin_system"), "source_update_year": dv["source_update_year"],
                       "ingest_id": dv["ingest_id"], "registered_at": dv["registered_at"], "tool": dv["tool"],
                       "origin_path": dv["origin_path"] if allowed else None, "ingested_from": dv["ingested_from"] if allowed else None},
        "metadata_completeness": query_completeness(data_root, ds, ver),
        "related_datasets": [{"dataset_id": s["layer_dataset_id"], "layer_name": s["layer_name"], "catalog_role": s["role"],
                              "published": bool(s["published"]), "relation": "same_asset" if s["source_asset_id"] == layer["source_asset_id"] else "same_package"}
                             for s in siblings],
        "field_relations": joins,
        "usage_note": USAGE_NOTE,
        "links": _links(dataset_id, dsv),
    }


# --------------------------------------------------------------------------- anlamsal sözlük ve domainler

def semantic_dictionary(data_root: Path, dataset_id: str, version: str | None = None) -> dict[str, Any]:
    layer = _layer(data_root, dataset_id, version)
    dsv = layer["dataset_version"]
    rows = _query(data_root, "SELECT * FROM semantic_fields WHERE layer_dataset_id=? AND version=? ORDER BY rowid", (dataset_id, dsv))
    for r in rows:
        r["domain_link"] = _links(dataset_id, dsv)["domain"].replace("{domain}", r["domain_name"]) if r["domain_name"] else None
    log = _one(_query(data_root, "SELECT value FROM meta WHERE key='verification_log'"))
    return {"contract": CONTRACT, "dataset_id": dataset_id, "version": dsv, "version_id": version_id(dataset_id, dsv),
            "content_id": layer["content_id"], "statuses": list(semantics.STATUSES), "fields": rows,
            "verification_log": json.loads(log["value"]) if log else None,
            "conflicts": _query(data_root, "SELECT * FROM metadata_conflicts WHERE dataset_id=? AND version=?", (layer["dataset_id"], layer["version"])),
            "source_priority": semantics.METADATA_SOURCE_PRIORITY,
            "note": "status yalnız 'meaning' içindir; unit_status ve null_meaning_status ayrıdır. Tip bilgisi anlam sayılmaz. "
                    "verified_by bir beyandır; teknik olarak doğrulanan yalnız yetkili yerel kanaldır (verification_auth)."}


def field_info(data_root: Path, dataset_id: str, version: str | None, name: str) -> dict[str, Any]:
    layer = _layer(data_root, dataset_id, version)
    dsv = layer["dataset_version"]
    row = _one(_query(data_root, "SELECT * FROM semantic_fields WHERE layer_dataset_id=? AND version=? AND field=?", (dataset_id, dsv, name)))
    if row is None:
        raise NotFound("field_not_found")
    row["content_id"] = layer["content_id"]
    dom = None
    if row["domain_name"]:
        d = _one(_query(data_root, "SELECT * FROM domains WHERE dataset_id=? AND version=? AND name=?", (layer["dataset_id"], layer["version"], row["domain_name"])))
        dom = {"name": row["domain_name"], "defined_in_source": d is not None, "description": (d or {}).get("description"),
               "code_count": (d or {}).get("code_count"), "domain_origin": row["domain_origin"],
               "link": _links(dataset_id, dsv)["domain"].replace("{domain}", row["domain_name"])}
    joins = _query(data_root, "SELECT * FROM field_relations WHERE dataset_id=? AND version=? AND ((from_layer_dataset_id=? AND from_field=?) "
                              "OR (to_layer_dataset_id=? AND to_field=?))", (layer["dataset_id"], layer["version"], dataset_id, name, dataset_id, name))
    return {"contract": CONTRACT, "version_id": version_id(dataset_id, dsv), **row, "domain": dom, "field_relations": joins}


def domain_codes(data_root: Path, dataset_id: str, version: str | None, name: str) -> dict[str, Any]:
    layer = _layer(data_root, dataset_id, version)
    ds, ver = layer["dataset_id"], layer["version"]
    d = _one(_query(data_root, "SELECT * FROM domains WHERE dataset_id=? AND version=? AND name=?", (ds, ver, name)))
    if d is None:
        raise NotFound("domain_not_found")
    codes = _query(data_root, "SELECT code, label FROM domain_values WHERE dataset_id=? AND version=? AND domain=? ORDER BY rowid", (ds, ver, name))
    used = _query(data_root, "SELECT layer_dataset_id, version AS dataset_version, field FROM semantic_fields WHERE dataset_id=? AND domain_name=? "
                             "AND version IN (SELECT dataset_version FROM package_layers WHERE package_dataset_id=? AND package_version=?) "
                             "ORDER BY layer_dataset_id, field", (ds, name, ds, ver))
    conflicts = _query(data_root, "SELECT item, authoritative_value, other_value, other_source, issue FROM metadata_conflicts "
                                  "WHERE dataset_id=? AND version=? AND kind='domain_label' AND subject=?", (ds, ver, name))
    return {"contract": CONTRACT, "package_dataset_id": ds, "version": ver, "package_version": ver, "name": name, "description": d["description"],
            "domain_type": d["domain_type"], "field_type": d["field_type"], "code_count": d["code_count"], "source": d["source_file"],
            "source_priority": semantics.METADATA_SOURCE_PRIORITY["domain_labels"],
            "codes": codes, "used_by_fields": used, "conflicts": conflicts,
            "note": "Kodlar ve açıklamalar yetkili kaynaktaki tanımla birebirdir; çevrilmez, düzeltilmez veya tahmin edilmez. "
                    "İkincil kaynaklardaki farklı/bozuk etiketler 'conflicts' altında yalnız raporlanır."}


# --------------------------------------------------------------------------- kalite, köken, erişim

@functools.lru_cache(maxsize=64)
def _record_ids(gdb: str, layer: str, fids: tuple[int, ...], fields: tuple[str, ...]) -> dict[int, dict[str, Any]]:
    from .vector_inspect import read_records

    return read_records(Path(gdb), layer, list(fids), list(fields))


def quality(data_root: Path, dataset_id: str, version: str | None = None) -> dict[str, Any]:
    layer = _layer(data_root, dataset_id, version)
    ds, ver = layer["dataset_id"], layer["version"]
    names = {r["layer_name"] for r in _query(data_root, "SELECT layer_name FROM layers WHERE dataset_id=? AND version=?", (ds, ver))}
    findings = [f for f in _query(data_root, "SELECT * FROM quality_findings WHERE dataset_id=? AND version=? ORDER BY seq", (ds, ver))
                if f["layer_dataset_id"] == dataset_id or f["subject"] not in names]
    special = [f for f in findings if f["category"] == "INTENTIONAL_NULL_IDENTIFIER" and f["layer_dataset_id"] == dataset_id]
    ids: dict[int, dict[str, Any]] = {}
    schema = {r["name"] for r in _query(data_root, "SELECT name FROM fields WHERE layer_dataset_id=? AND version=?", (dataset_id, ver))}
    wanted = tuple(f for f in ("GLOBALID", "TANITIMKODU") if f in schema)
    asset, folder = _physical(data_root, layer)
    if special and wanted and asset:
        try:  # salt okunur GDAL okuması; hata olursa kimlikler bilinmiyor kalır
            ids = _record_ids(str(folder / "payload" / asset["path"]), layer["layer_name"], tuple(sorted(f["oid_a"] for f in special)), wanted)
        except Exception:
            ids = {}
    gates = _query(data_root, "SELECT gate_id, status, detail, source_file FROM quality_gates WHERE dataset_id=? AND version=?", (ds, ver))
    dv = _one(_query(data_root, "SELECT quality_status FROM dataset_versions WHERE dataset_id=? AND version=?", (ds, ver)))
    return {
        "contract": CONTRACT, "version_id": version_id(dataset_id, layer["dataset_version"]), "package_version": ver, "status": dv["quality_status"],
        "status_policy": "Durum otomatik yükseltilmez; QA_CONDITIONAL bir kullanım koşuludur, otomatik ret değildir.",
        "gates": gates, "findings": findings, "summary": finding_summary(data_root, ds, ver),
        "special_records": [{"objectid": f["oid_a"], "name": f["feature_a"], "classification": "DOCUMENTED_SPECIAL_RECORD",
                             "reason": f["evidence"], "disposition": f["disposition"], "finding_seq": f["seq"],
                             **{k.lower(): ids.get(f["oid_a"], {}).get(k) for k in wanted},
                             "identifier_note": "OBJECTID taşınabilir kimlik değildir; GLOBALID kaynaktaki kalıcı nesne kimliğidir."}
                            for f in special],
        "record_issues": query_record_issues(data_root, ds, ver),
    }


def lineage(data_root: Path, dataset_id: str, version: str | None = None, *, authorized: bool = False) -> dict[str, Any]:
    layer = _layer(data_root, dataset_id, version)
    ds, ver = layer["dataset_id"], layer["version"]
    rels = _query(data_root, "SELECT subject, predicate, object, evidence FROM relations WHERE dataset_id=? AND version=?", (ds, ver))
    dv = _one(_query(data_root, "SELECT * FROM dataset_versions WHERE dataset_id=? AND version=?", (ds, ver)))
    allowed = authorized or dv["access_class"] == "PUBLIC"
    return {"contract": CONTRACT, "version_id": version_id(dataset_id, layer["dataset_version"]),
            "content": {"content_id": layer["content_id"], "algorithm": layer["content_algorithm"], "same_content_as": layer["same_content_as"]},
            "package": {"package_id": dv["package_id"], "package_version": ver, "fingerprint_sha256": dv["package_fingerprint"], "status": dv["package_status"]},
            "present_in_packages": layer["present_in_packages"],
            "registration": {"ingest_id": dv["ingest_id"], "registered_at": dv["registered_at"], "tool": dv["tool"], "gdal": dv["gdal"]},
            "origin_path": dv["origin_path"] if allowed else None,
            "relations": [r for r in rels if r["subject"] == dataset_id or r["predicate"] in ("registered_from_package", "part_of_version", "documents")],
            "field_relations": _query(data_root, "SELECT * FROM field_relations WHERE dataset_id=? AND version=?", (ds, ver)),
            "derived_artifacts": [r for r in rels if r["predicate"] == "derived_from"]}


def access(data_root: Path, dataset_id: str, version: str | None = None, *, authorized: bool = False) -> dict[str, Any]:
    layer = _layer(data_root, dataset_id, version)
    dv = _one(_query(data_root, "SELECT * FROM dataset_versions WHERE dataset_id=? AND version=?", (layer["dataset_id"], layer["version"])))
    meta = _one(_query(data_root, "SELECT * FROM layer_metadata WHERE layer_dataset_id=? AND version=?", (dataset_id, layer["version"])))
    asset, folder = _physical(data_root, layer)
    conditions = _access_conditions(dv, meta, authorized)
    allowed = conditions["physical_access"] == "available"
    storage = storage_block(data_root, layer["dataset_id"], layer["version"], asset, allowed)
    local = storage["backend"] == "local"
    return {"contract": CONTRACT, "version_id": version_id(dataset_id, layer["dataset_version"]), "package_version": layer["version"],
            "content_id": layer["content_id"], "asset_id": layer["source_asset_id"], "format": (asset or {}).get("format"), "layer": layer["layer_name"],
            "reader": READERS.get((asset or {}).get("format")), "relative_path": f"payload/{asset['path']}" if asset else None,
            # Bulut sürümünde yerel yol yoktur (uydurulmaz): erişim ``storage`` bloğundadır.
            "absolute_path": str(folder / "payload" / asset["path"]) if (asset and allowed and local) else None,
            "storage": storage, "read_only": True, "conditions": conditions, "usage_note": USAGE_NOTE}


# --------------------------------------------------------------------------- sürüm farkları

def diff(data_root: Path, dataset_id: str, from_version: str, to_version: str) -> dict[str, Any]:
    a, b = _layer(data_root, dataset_id, from_version), _layer(data_root, dataset_id, to_version)

    def fields(layer: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {r["field"]: r for r in _query(data_root, "SELECT * FROM semantic_fields WHERE layer_dataset_id=? AND version=?",
                                              (dataset_id, layer["dataset_version"]))}

    def codes(layer: dict[str, Any], name: str | None) -> dict[str, str]:
        if not name:
            return {}
        return {r["code"]: r["label"] for r in _query(data_root, "SELECT code, label FROM domain_values WHERE dataset_id=? AND version=? AND domain=?",
                                                      (layer["dataset_id"], layer["version"], name))}

    fa, fb = fields(a), fields(b)
    changes: list[dict[str, Any]] = []
    for name in sorted(set(fa) | set(fb)):
        x, y = fa.get(name), fb.get(name)
        if x is None or y is None:
            changes.append({"field": name, "change": "added" if x is None else "removed"})
            continue
        for key, kind in (("data_type", "type_changed"), ("domain_name", "domain_changed"), ("unit", "unit_changed"),
                          ("meaning", "meaning_changed"), ("status", "semantic_status_changed")):
            if x.get(key) != y.get(key):
                changes.append({"field": name, "change": kind, "from": x.get(key), "to": y.get(key)})
        ca, cb = codes(a, x.get("domain_name")), codes(b, y.get("domain_name"))
        if x.get("domain_name") and x.get("domain_name") == y.get("domain_name") and ca != cb:
            changes.append({"field": name, "change": "domain_codes_changed", "domain": x["domain_name"],
                            "added": sorted(set(cb) - set(ca)), "removed": sorted(set(ca) - set(cb)),
                            "relabelled": sorted(k for k in set(ca) & set(cb) if ca[k] != cb[k])})
    layer_changes = [{"attribute": k, "from": a[k], "to": b[k]} for k in ("feature_count", "geometry_type", "crs_name") if a[k] != b[k]]
    same = a["dataset_version"] == b["dataset_version"]
    return {"contract": CONTRACT, "dataset_id": dataset_id, "from": version_id(dataset_id, a["dataset_version"]),
            "to": version_id(dataset_id, b["dataset_version"]), "same_dataset_version": same,
            "content_identical": bool(a["content_id"]) and a["content_id"] == b["content_id"],
            "field_changes": changes, "layer_changes": layer_changes, "unchanged": not changes and not layer_changes}
