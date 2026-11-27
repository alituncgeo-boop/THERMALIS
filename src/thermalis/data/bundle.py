"""ZIP aktarım paketleri (source bundle): genel kayıt bağdaştırıcısı.

Paket biçimi (ör. HD06/HD08/HD10/HD12/HD14/HD26 teslimleri):

    <paket>/<paket>.zip                 özgün arşiv (deflate/stored) — ÇIKARILMADAN yayımlanır
    <paket>/<paket>_ZIP_SHA256.txt      arşivin dış SHA-256'sı (yan dosya)
    <paket>/*.md|*.json|*.txt|*.py      teslim belgeleri
    ZIP içinde: MANIFEST.json (``files``: path/bytes/sha256), RELEASE_STATUS.json, kaynak klasörleri

Genel biçim işleme ile bilimsel yorum ayrıdır:
- Bütünlük: dış ZIP = yan dosya; her iç üye akışla açılıp iç manifestle (boyut + SHA-256) karşılaştırılır;
  manifestte olmayan üye reddedilir. Dış belgelerin özeti kayıt sırasında hesaplanır ve böyle etiketlenir.
- Keşif (``/vsizip/`` ile, çıkarma yok): File Geodatabase katmanları (geometri, sayım, CRS, kapsam, şema, boş değer,
  içerik kimliği), GeoTIFF'ler (boyut, veri tipi, nodata, çözünürlük, yatay/dikey CRS), beyan edilen tablolar (CSV profili).
- İç içe arşivler (ör. özgün indirme ZIP'leri) açılmaz; üye olarak doğrulanır ve kaydedilir.
- Bilimsel anlam (kalite durumu, sınırlamalar, zaman anlamı, erişim) paket dışı BEYANDAN ve paketin kendi kayıtlarından
  gelir; yükseltilmez, uydurulmaz.

Arşiv bayt bayt yayımlanır; tek üye (veya GDB klasörü) bulutta aralık okuma + yerel açma ile malzemeleştirilir.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from ..storage import read_json, write_json_atomic
from . import declaration as decl
from .collection import (RECORDS_DIR, BuiltCollection, CollectionError, SourcePackage, _gdal_version, _sha256_file, _stat_sig,
                         _utc_now, canonical, index_file)
from .formats.raster import RASTER_SUFFIXES, inspect_raster
from .formats.tabular import profile_csv
from .formats.zip_index import ZipIndexError, index_zip, vsizip
from .integrity import ManifestEntry

BUNDLE_SCHEMA = "thermalis.source-bundle/1"
TOOL = "thermalis-bundle/1"
SIDECAR_SUFFIX = "_ZIP_SHA256.txt"
DOC_SUFFIXES = (".json", ".md", ".csv", ".txt")
MAX_RECORD_DOC = 2 * 1024 * 1024
RECORD_NAMES = ("version.json", "quality.json", "integrity.json", "bundle.json", "THERMALIS_DECLARATION.json")


def is_bundle(root: Path) -> bool:
    root = Path(root)
    return root.is_dir() and not (root / "PACKAGE" / "MANIFEST.json").is_file() \
        and len(list(root.glob("*.zip"))) == 1 and len(list(root.glob(f"*{SIDECAR_SUFFIX}"))) == 1


def _inner_manifest_name(names: list[str]) -> str:
    top = [n for n in names if n == "MANIFEST.json"]
    if not top:
        raise CollectionError("inner_manifest_missing", "MANIFEST.json (ZIP kökü)")
    return top[0]


def load_bundle(root: Path) -> SourcePackage:
    """Paketi okur (yalnız okuma). Arşivin dış özeti yan dosyadan beklenir; doğrulama kayıt sırasında akışla yapılır."""
    import zipfile

    root = Path(root).resolve()
    if not is_bundle(root):
        raise CollectionError("not_a_zip_bundle", root.name)
    archive = next(root.glob("*.zip"))
    sidecar = next(root.glob(f"*{SIDECAR_SUFFIX}"))
    parts = sidecar.read_text(encoding="utf-8-sig").split()
    if not parts or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
        raise CollectionError("sidecar_unreadable", sidecar.name)
    if len(parts) > 1 and parts[-1].lstrip("*") != archive.name:
        raise CollectionError("sidecar_names_other_file", parts[-1])
    try:
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            manifest_bytes = zf.read(_inner_manifest_name(names))
    except zipfile.BadZipFile as exc:
        raise CollectionError("not_a_zip", str(exc)) from None
    try:
        manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    except ValueError:
        raise CollectionError("inner_manifest_unreadable") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        raise CollectionError("inner_manifest_file_list_missing", "files")
    entries, origin = [], {}
    for p in sorted(root.iterdir()):
        if p.is_symlink() or not p.is_file():
            raise CollectionError("unexpected_entry_in_bundle", p.name)
        if p == archive:
            entries.append(ManifestEntry(p.name, p.stat().st_size, parts[0].lower()))
            origin[p.name] = "sidecar_sha256"
        else:
            digest, size = _sha256_file(p)
            entries.append(ManifestEntry(p.name, size, digest))
            origin[p.name] = "computed_at_registration"
    meta = {k: v for k, v in manifest.items() if not isinstance(v, (list, dict))}
    package_id = root.name
    return SourcePackage(root, package_id, hashlib.sha256(manifest_bytes).hexdigest(), meta, entries, [], {},
                         kind="zip_bundle", archive=archive.name, checksum_origin=origin)


# --------------------------------------------------------------------------- üye dizini (günlüklü)

def _zip_index(pkg: SourcePackage, work: Path, cancel, progress) -> dict[str, Any]:
    archive = pkg.path(pkg.archive)
    journal = work / "index" / ("zip-" + hashlib.sha256(pkg.archive.encode("utf-8")).hexdigest()[:24] + ".json")
    sig = _stat_sig(archive)
    if journal.is_file():
        cached = read_json(journal)
        if cached.get("stat") == sig and cached.get("tool") == TOOL and cached.get("manifest_sha256") == pkg.manifest_sha256:
            return cached
    try:
        idx = index_zip(archive, cancel=cancel, progress=progress)
    except ZipIndexError as exc:
        raise CollectionError(exc.code, exc.detail) from None
    if _stat_sig(archive) != sig:
        raise CollectionError("source_changed_during_indexing", pkg.archive)
    record = {"tool": TOOL, "stat": sig, "manifest_sha256": pkg.manifest_sha256, "archive_bytes": idx.archive_bytes,
              "members": [m.as_dict() for m in idx.members], "indexed_at": _utc_now()}
    write_json_atomic(journal, record)
    return record


def _verify_against_inner_manifest(pkg: SourcePackage, members: dict[str, dict]) -> tuple[int, str]:
    import zipfile

    with zipfile.ZipFile(pkg.path(pkg.archive)) as zf:
        manifest_name = _inner_manifest_name(zf.namelist())
        manifest = json.loads(zf.read(manifest_name).decode("utf-8-sig"))
    listed = {}
    for f in manifest["files"]:
        try:
            listed[str(f["path"])] = (int(f["bytes"]), str(f["sha256"]).lower())
        except (KeyError, TypeError, ValueError):
            raise CollectionError("inner_manifest_entry_invalid") from None
    problems = [p for p, (size, sha) in listed.items() if p not in members or members[p]["size"] != size or members[p]["sha256"] != sha]
    if problems:
        raise CollectionError("inner_member_sha256_mismatch", ", ".join(problems[:5]))
    unlisted = sorted(set(members) - set(listed) - {manifest_name})
    if unlisted:
        raise CollectionError("member_not_in_inner_manifest", ", ".join(unlisted[:5]))
    return len(listed), manifest_name


# --------------------------------------------------------------------------- keşif

def _layer_key(name: str) -> str:
    from ..text import match_key

    return re.sub(r"[^A-Z0-9]+", "_", match_key(name).upper()).strip("_") or "LAYER"


def _discover(pkg: SourcePackage, members: dict[str, dict], spec: dict[str, Any], log, work: Path | None = None) -> dict[str, Any]:
    """Keşif sonucu çalışma klasöründe günlüklenir (arşiv durumu + beyanlı tablolar anahtarıyla; roller sonradan uygulanır):
    büyük katmanlarda (ör. 1,2 milyon bina) beyan değişince akışlar yeniden okunmaz."""
    roles = spec.get("roles") or {}
    journal = None
    if work is not None:
        key = hashlib.sha256(json.dumps({"archive": pkg.archive, "stat": _stat_sig(pkg.path(pkg.archive)), "tool": TOOL,
                                         "manifest": pkg.manifest_sha256, "tables": sorted(spec.get("tables") or [])},
                                        sort_keys=True).encode("utf-8")).hexdigest()[:24]
        journal = work / "index" / f"discovery-{key}.json"
        if journal.is_file():
            found = read_json(journal)
            return _apply_roles(found, roles)
    found = _discover_uncached(pkg, members, spec, log)
    if journal is not None:
        write_json_atomic(journal, found)
    return _apply_roles(found, roles)


def _apply_roles(found: dict[str, Any], roles: dict[str, str]) -> dict[str, Any]:
    for v in found["vector"]:
        v["role"] = roles.get(v["layer"]) or roles.get(v["container"])
    for r in found["raster"] + found["table"]:
        r["role"] = roles.get(r["member"])
    return found


def _discover_uncached(pkg: SourcePackage, members: dict[str, dict], spec: dict[str, Any], log) -> dict[str, Any]:
    from .vector_inspect import inspect_vector, layer_facts

    archive_path = pkg.path(pkg.archive).as_posix()
    out: dict[str, Any] = {"vector": [], "raster": [], "table": [], "nested_archives": [], "wkt": {}}
    gdbs = sorted({n.split(".gdb/")[0] + ".gdb" for n in members if ".gdb/" in n})
    roles: dict[str, str] = {}
    for gdb in gdbs:
        path = vsizip(archive_path, gdb)
        if log:
            log(f"vector: {gdb}")
        info = inspect_vector(path)
        for lname, layer in info["layers"].items():
            facts = layer_facts(path, lname)
            if layer.get("crs_wkt"):
                out["wkt"][hashlib.sha256(layer["crs_wkt"].encode("utf-8")).hexdigest()] = layer["crs_wkt"]
            out["vector"].append({"container": gdb, "layer": lname, "role": roles.get(lname) or roles.get(gdb),
                                  **{k: v for k, v in layer.items() if k != "crs_wkt"},
                                  "crs_wkt_sha256": hashlib.sha256(layer["crs_wkt"].encode("utf-8")).hexdigest() if layer.get("crs_wkt") else None,
                                  "content": {"content_id": facts["content_id"], "algorithm": facts["algorithm"]},
                                  "domains_used": sorted({f["domain"] for f in layer["fields"] if f.get("domain")})})
        out.setdefault("domains", {})[gdb] = info.get("domains", {})
    for name in sorted(members):
        low = name.lower()
        if ".gdb/" in low:
            continue
        if low.endswith(RASTER_SUFFIXES):
            if log:
                log(f"raster: {name}")
            r = inspect_raster(vsizip(archive_path, name))
            crs = r.get("crs") or {}
            if crs.get("wkt"):
                out["wkt"][crs["wkt_sha256"]] = crs.pop("wkt")
            out["raster"].append({"member": name, "role": roles.get(name), "raster": r})
        elif low.endswith((".zip", ".tar", ".gz", ".7z")):
            out["nested_archives"].append({"member": name, "bytes": members[name]["size"], "sha256": members[name]["sha256"],
                                           "note": "not opened (preserved and verified as a member)"})
    import zipfile

    with zipfile.ZipFile(pkg.path(pkg.archive)) as zf:
        for name in spec.get("tables") or []:
            if name not in members:
                raise CollectionError("declared_table_not_in_archive", name)
            if log:
                log(f"table: {name}")
            with zf.open(name) as stream:
                out["table"].append({"member": name, "role": roles.get(name), "profile": profile_csv(stream)})
    return out


def _primary_section(profile: dict[str, Any]) -> dict[str, Any] | None:
    return max(profile["sections"], key=lambda s: s["records"]) if profile["sections"] else None


def _temporal(spec: dict[str, Any] | None, found: dict[str, Any], resolve) -> dict[str, Any] | None:
    """Zaman kapsamı ANLAMIYLA (gözlem dönemi, edinim aralığı, referans dönemi …). Kaynakta yoksa ``unknown``."""
    if not spec:
        return None
    semantics = spec.get("semantics") or "unknown"
    out: dict[str, Any] = {"semantics": semantics, "start": None, "end": None, "basis": None,
                           "time_reference": spec.get("time_reference")}
    if spec.get("derive_from_table"):
        d = spec["derive_from_table"]
        table = next((t for t in found["table"] if t["member"] == d.get("member")), None)
        section = _primary_section(table["profile"]) if table else None
        col = next((c for c in (section or {}).get("columns", []) if c["name"] == d.get("column")), None)
        if col and col.get("temporal"):
            out.update(start=col["temporal"]["first_label"], end=col["temporal"]["last_label"],
                       basis=f"data:{d['member']}#{d['column']} (labels as in source; no offset applied)",
                       distinct_labels=col["temporal"]["distinct_labels"])
        else:
            out["basis"] = "declared_table_column_not_temporal"
    elif spec.get("start_ref") or spec.get("end_ref"):
        out.update(start=resolve(spec["start_ref"]) if spec.get("start_ref") else None,
                   end=resolve(spec["end_ref"]) if spec.get("end_ref") else None,
                   basis=f"package record: {spec.get('start_ref')} / {spec.get('end_ref')}")
    elif spec.get("value_ref"):
        out.update(start=resolve(spec["value_ref"]), basis=f"package record: {spec['value_ref']}")
    for k in ("note", "note_ref"):
        if spec.get(k):
            out[k] = spec[k] if k == "note" else resolve(spec[k])
    return out


# --------------------------------------------------------------------------- kayıtlar

def build_bundle_records(pkg: SourcePackage, declaration: dict[str, Any], work: Path, *, ingest_id: str,
                         cancel: Callable[[], None] | None = None, progress: Callable[[str, int, int], None] | None = None,
                         log: Callable[[str], None] | None = None) -> BuiltCollection:
    import zipfile

    from .. import __version__

    decl.validate(declaration, manifest_sha256=pkg.manifest_sha256, package_id=pkg.package_id)
    spec = declaration.get("bundle")
    if not isinstance(spec, dict):
        raise CollectionError("declaration_bundle_section_missing")
    pinned = work / RECORDS_DIR
    binding = {"manifest_sha256": pkg.manifest_sha256, "declaration_sha256": hashlib.sha256(canonical(declaration)).hexdigest(),
               "tool": TOOL}
    total = sum(e.bytes for e in pkg.entries)
    done = {"n": 0}

    def tick(n: int) -> None:
        done["n"] += n
        if progress is not None:
            progress("index", done["n"], total)

    # Dış dosyalar: arşiv yan dosyadaki özetle akışla doğrulanır (günlüklü; değişmezse yeniden okunmaz).
    for entry in pkg.entries:
        index_file(pkg, entry, work, interpreter=None, cancel=cancel, progress=tick)
    zidx = _zip_index(pkg, work, cancel, None)
    members = {m["name"]: m for m in zidx["members"]}
    if (pinned / "binding.json").is_file() and read_json(pinned / "binding.json") == binding \
            and all((pinned / n).is_file() for n in RECORD_NAMES):
        records = {n: (pinned / n).read_bytes() for n in RECORD_NAMES}
        for p in sorted((pinned / "package").rglob("*")) if (pinned / "package").is_dir() else []:
            if p.is_file():
                records["package/" + p.relative_to(pinned / "package").as_posix()] = p.read_bytes()
        b = json.loads(records["bundle.json"])
        return BuiltCollection(records, len(b["datasets"]), len(b["members"]), sum(1 for d in b["datasets"] if d["kind"] == "raster"),
                               list(b.get("issues", [])), True)
    listed, manifest_name = _verify_against_inner_manifest(pkg, members)
    started = _utc_now()

    with zipfile.ZipFile(pkg.path(pkg.archive)) as zf:
        def resolve(ref: str) -> Any:
            rel, _, pointer = ref.partition("#")
            if rel not in members:
                return None
            try:
                value: Any = json.loads(zf.read(rel).decode("utf-8-sig"))
            except ValueError:
                return None
            for part in [p for p in pointer.split(".") if p]:
                if not isinstance(value, dict) or part not in value:
                    return None
                value = value[part]
            return value

        found = _discover(pkg, members, spec, log, work)
        qd = declaration["quality"]
        source_status = resolve(qd["source_status_ref"])
        status, rule = decl.quality_from_source_status(source_status)
        cross = [{"ref": r, "value": resolve(r)} for r in qd.get("cross_check_refs", [])]
        limitations = resolve(qd["documented_limitations_ref"]) if qd.get("documented_limitations_ref") else None
        temporal = _temporal(declaration.get("temporal"), found, resolve)
        # Paketin kendi küçük belgeleri kayıtlara bayt bayt kopyalanır (bulut yansısında okunabilir; katalog yeniden kurulabilir).
        package_docs = {}
        for name, m in sorted(members.items()):
            if "/" not in name and name.lower().endswith(DOC_SUFFIXES) and m["size"] <= MAX_RECORD_DOC:
                data = zf.read(name)
                if hashlib.sha256(data).hexdigest() != m["sha256"]:
                    raise CollectionError("member_changed_during_registration", name)
                package_docs["package/" + name] = data

    issues: list[str] = []
    conflicts = [c for c in cross if c["value"] is not None and str(c["value"]) != str(source_status)]
    if conflicts:
        issues.append("source_quality_status_conflict:" + ",".join(c["ref"] for c in conflicts))
    if source_status is None:
        issues.append("source_quality_status_not_found:" + qd["source_status_ref"])
    layers: dict[str, Any] = {}
    containers: list[dict[str, str]] = []
    datasets: list[dict[str, Any]] = []
    used: set[str] = set()

    def unique(name: str) -> str:
        key, n = _layer_key(name), 2
        base = key
        while key in used:
            key, n = f"{base}_{n}", n + 1
        used.add(key)
        return key

    archive_name = pkg.archive
    for v in found["vector"]:
        name = unique(v["layer"])
        container = f"{archive_name}/{v['container']}"
        if container not in [c["path"] for c in containers]:
            containers.append({"path": container, "format": "filegdb_vector"})
        layers[name] = {"role": v.get("role") or "source_layer", "published": True, "geometry_type": v["geometry_type"],
                        "feature_count": v["feature_count"], "expected_feature_count": v.get("header_feature_count"),
                        "crs_name": v.get("crs_name"), "crs_matches_package_wkt": None, "extent_native": v.get("extent"),
                        "null_geometry": v.get("null_geometry"), "empty_geometry": v.get("empty_geometry"),
                        "source": container, "source_layer_name": v["layer"],
                        "fields": [{**f, "null_count": v["null_counts"].get(f["name"])} for f in v["fields"]],
                        "content": v["content"]}
        datasets.append({"kind": "vector", "layer": name, "container": v["container"], "source_layer_name": v["layer"],
                         "crs_wkt_sha256": v.get("crs_wkt_sha256"), "domains_used": v["domains_used"]})
    for r in found["raster"]:
        name = unique(Path(r["member"]).stem)
        info = r["raster"]
        crs = info.get("crs") or {}
        container = f"{archive_name}/{r['member']}"
        containers.append({"path": container, "format": "geotiff"})
        b0 = (info.get("bands") or [{}])[0]
        layers[name] = {"role": r.get("role") or "raster", "published": True, "geometry_type": "Raster",
                        "feature_count": None, "expected_feature_count": None, "crs_name": crs.get("name"),
                        "crs_matches_package_wkt": None, "extent_native": info.get("extent"), "null_geometry": None,
                        "empty_geometry": None, "source": container, "fields": [],
                        "raster": {k: info.get(k) for k in ("readable", "driver", "width", "height", "band_count", "resolution",
                                                            "geotransform", "compression", "layout", "cloud_optimized", "tiled",
                                                            "bbox_wgs84")}
                        | {"dtype": b0.get("dtype"), "nodata": b0.get("nodata"), "unit_type": b0.get("unit_type"),
                           "embedded_scale": b0.get("embedded_scale"), "embedded_offset": b0.get("embedded_offset"),
                           "epsg": crs.get("epsg"), "vertical_crs": crs.get("vertical_crs"), "crs_wkt_sha256": crs.get("wkt_sha256")},
                        "content": {"content_id": members[r["member"]]["sha256"], "algorithm": "sha256(member bytes)"}}
        datasets.append({"kind": "raster", "layer": name, "member": r["member"], "raster": info})
    for t in found["table"]:
        name = unique(Path(t["member"]).stem)
        container = f"{archive_name}/{t['member']}"
        containers.append({"path": container, "format": "csv_table"})
        section = _primary_section(t["profile"]) or {"columns": [], "records": 0}
        layers[name] = {"role": t.get("role") or "table", "published": True, "geometry_type": "None",
                        "feature_count": section["records"], "expected_feature_count": None, "crs_name": None,
                        "crs_matches_package_wkt": None, "extent_native": None, "null_geometry": None, "empty_geometry": None,
                        "source": container,
                        "fields": [{"name": c["name"], "type": "Real" if c["numeric_count"] and not c["text_count"] else
                                    ("DateTimeLabel" if c.get("temporal") else "String"), "alias": None, "width": None,
                                    "nullable": None, "domain": None, "null_count": c["empty_count"],
                                    "unit": c["header_declared_unit"]} for c in section["columns"]],
                        "table": {"delimiter": t["profile"]["delimiter"], "bom": t["profile"]["bom"],
                                  "sections": len(t["profile"]["sections"]), "physical_lines": t["profile"]["physical_lines"]},
                        "content": {"content_id": members[t["member"]]["sha256"], "algorithm": "sha256(member bytes)"}}
        datasets.append({"kind": "table", "layer": name, "member": t["member"], "profile": t["profile"]})
    if not layers:
        raise CollectionError("no_scientific_content_discovered", "GDB, raster veya beyan edilmiş tablo bulunamadı")

    wkt_by_hash = found["wkt"]
    crs_names = {l["crs_name"] for l in layers.values() if l.get("crs_name")}
    single_crs = next(iter(crs_names)) if len(crs_names) == 1 else None
    single_wkt = next(iter(wkt_by_hash.values())) if len(wkt_by_hash) == 1 else None
    documents = sorted([e.path for e in pkg.entries if e.path != archive_name] + list(package_docs))
    src = declaration["source"]
    checks = [
        {"id": "ZIP-01", "title": "Arşiv dış SHA-256'sı yan dosyayla aynı (akışla)", "status": "PASS", "detail": archive_name},
        {"id": "MAN-01", "title": "Her iç üye iç manifestle (boyut + SHA-256) aynı; listelenmemiş üye yok", "status": "PASS",
         "detail": f"{listed} üye · manifest {manifest_name}"},
        {"id": "DEC-01", "title": "Paket dışı beyan iç manifest SHA-256'sına bağlı", "status": "PASS", "detail": pkg.manifest_sha256},
        {"id": "DSC-01", "title": "Bilimsel içerik çıkarılmadan keşfedildi (/vsizip/)", "status": "PASS",
         "detail": f"vektör {len(found['vector'])} · raster {len(found['raster'])} · tablo {len(found['table'])} · iç arşiv {len(found['nested_archives'])}"},
        {"id": "QA-01", "title": "Kaynak QA durumu korundu; THERMALIS kalite ekseni türetildi", "status": "PASS" if source_status else "WARN",
         "detail": f"{source_status} → {status} ({rule})"},
    ]
    version_record = {
        "schema_version": "1.0", "record_kind": "source_bundle",
        "dataset_id": declaration["dataset_id"], "version": declaration["version"],
        "title": {"tr": None, "en": None, "label": " · ".join(declaration["dataset_id"].split("/")[1:])},
        "family": pkg.package_id.split("_", 1)[0],
        "source": {"package_id": pkg.package_id, "package_type": pkg.manifest_meta.get("artifact_kind"),
                   "package_built_at_utc": None, "package_status": str(source_status) if source_status is not None else None,
                   "owner": src.get("organization"), "product": src.get("product"), "citation": src.get("citation"),
                   "doi": src.get("doi"), "source_update_year": None, "origin_path": None, "ingested_from": str(pkg.root),
                   "package_fingerprint": pkg.manifest_sha256, "fingerprint_basis": "sha256(inner MANIFEST.json)",
                   "temporal_coverage": temporal, "declaration": "records/THERMALIS_DECLARATION.json"},
        "access": {"classification": declaration["access_class"], "public_release": None,
                   "authorization": declaration.get("authorization_reference"), "policy": declaration.get("access_basis"),
                   "components": {"source_products": declaration["access_class"],
                                  "supplementary_records": declaration.get("supplementary_records")}},
        "storage": {"payload": "payload", "archive": archive_name, "vector_sources": [], "containers": containers,
                    "bundle": "records/bundle.json", "read_only": True, "mode": "direct_publication_from_source_folder"},
        "crs": {"native_crs_name": single_crs, "native_wkt": single_wkt, "factory_code": None,
                "unit": None, "policy": "source native CRS preserved; no reprojection" + ("" if single_crs else "; multiple CRS"),
                "transformed": False},
        "layers": layers, "domains": [], "data_dictionary": [], "documents": documents, "derived_artifacts": [], "checks": checks,
        "registration": {"status": "REGISTERED", "ingest_id": ingest_id, "started_at": started, "registered_at": _utc_now(),
                         "tool": f"thermalis {__version__} ({TOOL})", "gdal": _gdal_version(), "mode": "records_only_direct_publication"},
    }
    quality_record = {
        "schema_version": "1.0", "status": status, "source_status": source_status, "source_status_ref": qd["source_status_ref"],
        "source_status_mapping": rule, "source_status_cross_checks": cross, "documented_limitations": limitations,
        "documented_limitations_ref": qd.get("documented_limitations_ref"), "limitations_documents": qd.get("limitations_documents", []),
        "gates": [{"id": "SOURCE-QA", "status": str(source_status), "detail": qd["source_status_ref"]}],
        "gates_source": qd["source_status_ref"], "exceptions": [], "layer_status": [], "item_issues": issues,
        "note": "Kaynak QA durumu ve belgelenmiş sınırlamalar olduğu gibi korunur; kayıt analiz uygunluğu beyan etmez.",
    }
    integrity_record = {
        "schema_version": "1.0", "algorithm": "SHA-256", "scope": "teslim klasöründeki tüm dosyalar (arşiv dahil, bayt bayt)",
        "files": [{"path": e.path, "bytes": e.bytes, "sha256": e.sha256, "checksum_origin": pkg.checksum_origin.get(e.path)}
                  for e in pkg.entries],
        "file_count": len(pkg.entries), "total_bytes": total,
        "verifications": {"source_package": {"status": "PASS", "checked_files": len(pkg.entries), "problems": [],
                                             "method": "streaming_sha256; archive vs sidecar; every inner member vs inner manifest"}},
        "members": {"archive": archive_name, "members_verified": listed},
    }
    bundle_record = {
        "schema": BUNDLE_SCHEMA, "dataset_id": declaration["dataset_id"], "version": declaration["version"],
        "package_id": pkg.package_id, "archive": archive_name, "inner_manifest": manifest_name,
        "inner_manifest_sha256": pkg.manifest_sha256, "archive_sha256": next(e.sha256 for e in pkg.entries if e.path == archive_name),
        "members": zidx["members"], "datasets": datasets, "nested_archives": found["nested_archives"],
        "domains": found.get("domains", {}), "crs_wkt": wkt_by_hash, "issues": issues,
        "access_methods": {"member": "/vsizip/{<archive>}/<member>", "materialize": "range read of compressed member + local inflate + sha256"},
        "value_policy": "source values and representations preserved; no unit conversion, interpolation or gap filling",
    }
    records = {"version.json": canonical(version_record), "quality.json": canonical(quality_record),
               "integrity.json": canonical(integrity_record), "bundle.json": canonical(bundle_record),
               "THERMALIS_DECLARATION.json": canonical(declaration), **package_docs}
    pinned.mkdir(parents=True, exist_ok=True)
    for name, data in records.items():
        target = pinned.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
    write_json_atomic(pinned / "binding.json", binding)
    return BuiltCollection(records, len(datasets), len(members), len(found["raster"]), issues, False)


def read_bundle(folder: Path) -> dict[str, Any] | None:
    path = folder / "bundle.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) and data.get("schema") == BUNDLE_SCHEMA else None
