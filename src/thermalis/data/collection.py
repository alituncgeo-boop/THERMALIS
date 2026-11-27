"""Raster koleksiyonu kaydı: kaynak klasöründen (kopyasız) dizin ve bilimsel kayıtlar.

Genel bağdaştırıcıdır: TAR arşivleri (üye konumu + üye SHA-256) ve bağımsız GeoTIFF/COG dosyaları. Ürüne özgü
yorum (ör. Landsat MTL) beyandaki ``collection.interpreter`` ile seçilen AYRI modüldedir (:mod:`.formats.landsat`).

Akış (yalnız okuma; kaynak paket değiştirilmez, çıkarılmaz, kopyalanmaz):

1. ``PACKAGE/MANIFEST.json`` okunur; her dosyanın varlığı ve boyutu denetlenir, listede olmayan dosya reddedilir.
2. Beyan (paket dışı, manifest özetine bağlı) doğrulanır.
3. Her dosya bir kez akış hâlinde okunur: SHA-256 manifestle karşılaştırılır; TAR ise aynı geçişte üye konumları ve
   üye SHA-256'ları çıkarılır. Raster üyeler ``/vsisubfile/`` ile başlıktan incelenir (kaynak değerler, ölçek uygulanmaz).
4. Sonuçlar bir dizin günlüğüne (çalışma klasörü) atomik yazılır: kesilen işlem yeniden başlatılınca tamamlanmış
   dosyalar yeniden okunmaz (boyut + değişiklik zamanı değişmediyse).
5. ``version.json`` / ``quality.json`` / ``integrity.json`` / ``collection.json`` üretilir ve çalışma klasöründe
   SABİTLENİR (yeniden başlatmada aynı baytlar yayımlanır).

Disk kullanımı: yalnız küçük JSON günlükleri (koleksiyon boyutundan bağımsız). Bellek: parça boyutuyla sınırlı.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..storage import read_json, write_json_atomic
from . import declaration as decl
from .formats import landsat
from .formats.preflight import preflight
from .formats.raster import RASTER_SUFFIXES, inspect_raster
from .formats.tar_index import TarIndexError, index_tar, vsisubfile
from .integrity import ManifestEntry
from .transfer_package import UNHASHED_PACKAGE_FILES

COLLECTION_SCHEMA = "thermalis.raster-collection/1"
JOURNAL_SCHEMA = "thermalis.collection-index-entry/1"
RECORDS_DIR = "records"
INDEX_DIR = "index"
TOOL = "thermalis-collection/1"
CHUNK = 8 * 1024 * 1024
RECORD_NAMES = ("version.json", "quality.json", "integrity.json", "collection.json", "THERMALIS_DECLARATION.json")


class CollectionError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


# --------------------------------------------------------------------------- kaynak paket

@dataclass
class SourcePackage:
    root: Path
    package_id: str
    manifest_sha256: str
    manifest_meta: dict[str, Any]
    entries: list[ManifestEntry]
    extra_files: list[str] = field(default_factory=list)  # manifestin kendisi gibi özetlenmeyen paket dosyaları
    # Alt küme görünümü: girdinin gerçek konumu (kopya YOK; kabul testleri asıl kaynak dosyaları okur).
    locations: dict[str, Path] = field(default_factory=dict)
    # "manifest_package" (PACKAGE/MANIFEST.json) | "zip_bundle" (ZIP + SHA-256 yan dosyası + iç manifest)
    kind: str = "manifest_package"
    archive: str | None = None
    checksum_origin: dict[str, str] = field(default_factory=dict)

    def path(self, rel: str) -> Path:
        return self.locations.get(rel) or self.root.joinpath(*rel.split("/"))

    @property
    def total_bytes(self) -> int:
        return sum(e.bytes for e in self.entries) + sum(self.path(r).stat().st_size for r in self.extra_files)


def _sha256_file(path: Path, cancel: Callable[[], None] | None = None) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            if cancel is not None:
                cancel()
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def load_source_package(root: Path) -> SourcePackage:
    """Manifesti okur ve klasörü manifestle (varlık + boyut) karşılaştırır. İçerik özeti burada okunmaz."""
    root = Path(root).resolve()
    manifest_path = root / "PACKAGE" / "MANIFEST.json"
    if not manifest_path.is_file():
        raise CollectionError("manifest_missing", "PACKAGE/MANIFEST.json")
    try:
        meta = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise CollectionError("manifest_unreadable", type(exc).__name__) from None
    rows = meta.get("sha256_files") if isinstance(meta, dict) else None
    if not isinstance(rows, list) or not rows:
        raise CollectionError("manifest_file_list_missing", "sha256_files")
    entries: list[ManifestEntry] = []
    seen: set[str] = set()
    for r in rows:
        try:
            path, size, sha = str(r["path"]), int(r["bytes"]), str(r["sha256"]).lower()
        except (KeyError, TypeError, ValueError):
            raise CollectionError("manifest_entry_invalid") from None
        parts = path.split("/")
        if not path or path.startswith("/") or "\\" in path or ".." in parts or ":" in parts[0] or len(sha) != 64 or size < 0:
            raise CollectionError("manifest_unsafe_path", path[:120])
        if path in seen:
            raise CollectionError("manifest_duplicate_path", path[:120])
        seen.add(path)
        entries.append(ManifestEntry(path, size, sha))
    declared = meta.get("file_count")
    if isinstance(declared, int) and declared != len(entries):
        raise CollectionError("manifest_file_count_mismatch", f"declared {declared}, listed {len(entries)}")
    on_disk = sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
    missing = [e.path for e in entries if not root.joinpath(*e.path.split("/")).is_file()]
    if missing:
        raise CollectionError("package_file_missing", ", ".join(missing[:5]))
    wrong = [e.path for e in entries if root.joinpath(*e.path.split("/")).stat().st_size != e.bytes]
    if wrong:
        raise CollectionError("package_file_size_mismatch", ", ".join(wrong[:5]))
    extra = [p for p in on_disk if p not in seen]
    unlisted = [p for p in extra if p not in UNHASHED_PACKAGE_FILES]
    if unlisted:
        raise CollectionError("package_file_not_in_manifest", ", ".join(unlisted[:5]))
    manifest_sha, _ = _sha256_file(manifest_path)
    package_id = str(meta.get("package_id") or meta.get("package") or root.name)
    return SourcePackage(root, package_id, manifest_sha, {k: v for k, v in meta.items() if k != "sha256_files"}, entries, extra)


def subset_package(src: SourcePackage, archives: list[str], out_dir: Path, package_id: str) -> SourcePackage:
    """Seçili arşivler + tüm paket belgelerinden oluşan alt küme (yalnız yeni bir MANIFEST.json yazılır; veri kopyalanmaz).

    Canlı kabul testleri için: gerçek kaynak dosyalar yerinde okunur; alt kümenin kendi manifest özeti olur.
    """
    by_path = {e.path: e for e in src.entries}
    missing = [a for a in archives if a not in by_path]
    if missing:
        raise CollectionError("archive_not_in_manifest", ", ".join(missing[:5]))
    data_suffixes = (".tar", ".tif", ".tiff")
    chosen = [by_path[a] for a in archives] + [e for e in src.entries if not e.path.lower().endswith(data_suffixes)]
    chosen = sorted({e.path: e for e in chosen}.values(), key=lambda e: e.path)
    meta = {**{k: v for k, v in src.manifest_meta.items() if k not in ("file_count", "package", "package_id")},
            "package_id": package_id, "subset_of": src.package_id, "subset_of_manifest_sha256": src.manifest_sha256,
            "file_count": len(chosen), "sha256_files": [{"path": e.path, "bytes": e.bytes, "sha256": e.sha256} for e in chosen]}
    manifest = Path(out_dir) / "PACKAGE" / "MANIFEST.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_bytes(canonical(meta))
    sha, _ = _sha256_file(manifest)
    return SourcePackage(Path(out_dir), package_id, sha, {k: v for k, v in meta.items() if k != "sha256_files"}, chosen,
                         ["PACKAGE/MANIFEST.json"], {e.path: src.path(e.path) for e in chosen})


# --------------------------------------------------------------------------- dosya başına dizin (günlüklü)

def _journal_path(work: Path, rel: str) -> Path:
    return work / INDEX_DIR / (hashlib.sha256(rel.encode("utf-8")).hexdigest()[:32] + ".json")


def _stat_sig(path: Path) -> dict[str, int]:
    st = path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _strip_wkt(raster: dict[str, Any], wkts: dict[str, str]) -> dict[str, Any]:
    crs = raster.get("crs") or {}
    if crs.get("wkt"):
        wkts.setdefault(crs["wkt_sha256"], crs.pop("wkt"))
    return raster


def _index_archive(pkg: SourcePackage, entry: ManifestEntry, interpreter: str | None,
                   cancel: Callable[[], None] | None, progress: Callable[[int], None] | None) -> dict[str, Any]:
    path = pkg.path(entry.path)
    wkts: dict[str, str] = {}
    try:
        with path.open("rb") as handle:
            idx = index_tar(handle, cancel=cancel, progress=progress)
    except TarIndexError as exc:
        raise CollectionError(exc.code, f"{entry.path}: {exc.detail}") from None
    if idx.archive_sha256 != entry.sha256 or idx.archive_size != entry.bytes:
        raise CollectionError("sha256_mismatch", entry.path)
    unsafe = [m.name for m in idx.members if not m.safe_name]
    files = idx.files()
    names = [m.name for m in files]

    def read_member(name: str) -> bytes:
        m = idx.member(name)
        if m.size > 16 * 1024 * 1024:
            raise CollectionError("metadata_member_too_large", name)
        with path.open("rb") as handle:
            handle.seek(m.offset)
            data = handle.read(m.size)
        if hashlib.sha256(data).hexdigest() != m.sha256:
            raise CollectionError("member_changed_during_indexing", name)
        return data

    interpretation = None
    if interpreter:
        detect, interpret, _ = landsat.INTERPRETERS[interpreter]
        if detect(names):
            interpretation = interpret(names, read_member)
    assets = []
    for m in files:
        raster = None
        if m.name.lower().endswith(RASTER_SUFFIXES):
            raster = _strip_wkt(inspect_raster(vsisubfile(m, path.as_posix())), wkts)
        interp = (interpretation or {}).get("assets", {}).get(m.name, {})
        # Arşiv alanları (konum/boyut/özet) yorumlayıcı alanlarından SONRA: hiçbir zaman ezilmez.
        assets.append({**interp, "member": m.name, "offset": m.offset, "bytes": m.size, "sha256": m.sha256, "raster": raster})
    return {"format": "tar", "tar_format": idx.format, "warnings": idx.warnings, "unsafe_member_names": unsafe,
            "non_file_members": [{"name": m.name, "type": m.type} for m in idx.members if m.type != "file"],
            "interpretation": {k: v for k, v in (interpretation or {}).items() if k != "assets"} or None,
            "assets": assets, "wkt": wkts}


def _index_raster_file(pkg: SourcePackage, entry: ManifestEntry, cancel, progress) -> dict[str, Any]:
    path = pkg.path(entry.path)
    sha, size = _sha256_file(path, cancel)
    if progress is not None:
        progress(size)
    if sha != entry.sha256 or size != entry.bytes:
        raise CollectionError("sha256_mismatch", entry.path)
    wkts: dict[str, str] = {}
    raster = _strip_wkt(inspect_raster(str(path)), wkts)
    return {"format": "geotiff", "warnings": [], "interpretation": None,
            "assets": [{"member": None, "offset": 0, "bytes": entry.bytes, "sha256": entry.sha256, "raster": raster}], "wkt": wkts}


def index_file(pkg: SourcePackage, entry: ManifestEntry, work: Path, *, interpreter: str | None,
               cancel: Callable[[], None] | None = None, progress: Callable[[int], None] | None = None) -> dict[str, Any]:
    """Tek dosyanın doğrulanmış dizini; günlükte geçerli kayıt varsa dosya yeniden okunmaz."""
    path = pkg.path(entry.path)
    journal = _journal_path(work, entry.path)
    sig = _stat_sig(path)
    if journal.is_file():
        try:
            cached = read_json(journal)
        except (OSError, ValueError):
            cached = None
        if isinstance(cached, dict) and cached.get("schema") == JOURNAL_SCHEMA and cached.get("path") == entry.path \
                and cached.get("sha256") == entry.sha256 and cached.get("stat") == sig and cached.get("interpreter") == interpreter \
                and cached.get("tool") == TOOL:
            if progress is not None:
                progress(entry.bytes)
            return cached
    low = entry.path.lower()
    if low.endswith(".tar"):
        body = _index_archive(pkg, entry, interpreter, cancel, progress)
    elif low.endswith((".tif", ".tiff")):
        body = _index_raster_file(pkg, entry, cancel, progress)
    else:
        sha, size = _sha256_file(path, cancel)
        if progress is not None:
            progress(size)
        if sha != entry.sha256 or size != entry.bytes:
            raise CollectionError("sha256_mismatch", entry.path)
        body = {"format": "document"}
    if _stat_sig(path) != sig:
        raise CollectionError("source_changed_during_indexing", entry.path)
    record = {"schema": JOURNAL_SCHEMA, "path": entry.path, "sha256": entry.sha256, "bytes": entry.bytes, "stat": sig,
              "interpreter": interpreter, "tool": TOOL, "verified": "streaming_sha256", "verified_at": _utc_now(), **body}
    write_json_atomic(journal, record)
    return record


# --------------------------------------------------------------------------- kayıtlar

@dataclass
class BuiltCollection:
    records: dict[str, bytes]
    item_count: int
    asset_count: int
    raster_asset_count: int
    issues: list[str]
    reused: bool


def _get(obj: dict[str, Any], dotted: str) -> Any:
    cur: Any = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _same(a: Any, b: Any) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def _package_catalog(pkg: SourcePackage, spec: dict[str, Any] | None) -> tuple[dict[str, dict[str, str]], str | None]:
    if not spec or not spec.get("path"):
        return {}, None
    rel = str(spec["path"])
    if rel not in {e.path for e in pkg.entries}:
        raise CollectionError("scene_catalog_not_in_manifest", rel)
    with pkg.path(rel).open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    col = str(spec.get("archive_column") or "relative_path")
    return {r.get(col, ""): r for r in rows}, rel


def build_records(pkg: SourcePackage, declaration: dict[str, Any], work: Path, *, ingest_id: str,
                  cancel: Callable[[], None] | None = None, progress: Callable[[str, int, int], None] | None = None,
                  log: Callable[[str], None] | None = None) -> BuiltCollection:
    """Kayıtları üretir veya (çalışma klasöründe sabitlenmişse) aynen yeniden kullanır."""
    from .. import __version__

    decl.validate(declaration, manifest_sha256=pkg.manifest_sha256, package_id=pkg.package_id)
    pinned = work / RECORDS_DIR
    binding = {"manifest_sha256": pkg.manifest_sha256, "declaration_sha256": hashlib.sha256(canonical(declaration)).hexdigest(),
               "tool": TOOL}
    if (pinned / "binding.json").is_file() and read_json(pinned / "binding.json") == binding \
            and all((pinned / n).is_file() for n in RECORD_NAMES):
        # Sabitlenmiş kayıtlar yeniden kullanılır; dosya günlükleri yine denetlenir (geçerliyse anında döner, değilse
        # dosya yeniden okunup manifestle doğrulanır). İçerik manifestle aynıysa kayıtlar geçerli kalır.
        for entry in sorted(pkg.entries, key=lambda e: e.path):
            index_file(pkg, entry, work, interpreter=declaration["collection"].get("interpreter"), cancel=cancel)
        records = {n: (pinned / n).read_bytes() for n in RECORD_NAMES}
        col = json.loads(records["collection.json"])
        return BuiltCollection(records, len(col["items"]), sum(len(i["assets"]) for i in col["items"]),
                               sum(1 for i in col["items"] for a in i["assets"] if a.get("raster")), list(col.get("issues", [])), True)

    pf = preflight([e.path for e in pkg.entries])
    if pf.route != "raster_collection":
        raise CollectionError("format_not_supported", f"route={pf.route} reasons={pf.reasons} unsupported={pf.unsupported[:5]}")
    col_decl = declaration["collection"]
    interpreter = col_decl.get("interpreter")
    total = sum(e.bytes for e in pkg.entries)
    done = {"n": 0}

    def tick(n: int) -> None:
        done["n"] += n
        if progress is not None:
            progress("index", done["n"], total)

    started = _utc_now()
    journals: dict[str, dict[str, Any]] = {}
    for entry in sorted(pkg.entries, key=lambda e: e.path):
        journals[entry.path] = index_file(pkg, entry, work, interpreter=interpreter, cancel=cancel, progress=tick)
        if log is not None:
            log(f"indexed {entry.path}")

    catalog_rows, catalog_rel = _package_catalog(pkg, col_decl.get("scene_catalog"))
    checks_spec = (col_decl.get("scene_catalog") or {}).get("checks") or {}
    items: list[dict[str, Any]] = []
    issues: list[str] = []
    wkts: dict[str, str] = {}
    data_paths = [p for p, j in sorted(journals.items()) if j["format"] in ("tar", "geotiff")]
    for rel in data_paths:
        j = journals[rel]
        wkts.update(j.get("wkt") or {})
        interp = j.get("interpretation") or {}
        item_id = interp.get("item_id") or rel.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        item_issues = list(interp.get("issues") or []) + [f"tar_warning:{w}" for w in j.get("warnings", [])]
        if j.get("unsafe_member_names"):
            item_issues.append(f"unsafe_member_names:{len(j['unsafe_member_names'])}")
        if interpreter and not interp:
            item_issues.append(f"interpreter_not_applicable:{interpreter}")
        unreadable = [a["member"] for a in j["assets"] if a.get("raster") and not a["raster"].get("readable")]
        if unreadable:
            item_issues.append(f"raster_unreadable:{len(unreadable)}")
        item = {"item_id": item_id, "archive": {"path": rel, "bytes": j["bytes"], "sha256": j["sha256"], "format": j["format"]},
                "properties": interp.get("properties") or {}, "bbox_wgs84": interp.get("bbox_wgs84"),
                "bbox_source": interp.get("bbox_source"), "footprint_wgs84": interp.get("footprint_wgs84"),
                "footprint_source": interp.get("footprint_source"), "metadata_members": interp.get("metadata_members"),
                "interpreter": interp.get("interpreter"), "assets": j["assets"], "package_properties": None, "issues": item_issues}
        if item["bbox_wgs84"] is None:
            boxes = [a["raster"].get("bbox_wgs84") for a in j["assets"] if a.get("raster") and a["raster"].get("bbox_wgs84")]
            if boxes:
                item["bbox_wgs84"] = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
                item["bbox_source"] = "raster_corner_transform_approximate"
        if catalog_rel:
            row = catalog_rows.get(rel)
            if row is None:
                item_issues.append("not_in_package_scene_catalog")
            else:
                item["package_properties"] = {"source": catalog_rel, **row}
                for column, dotted in checks_spec.items():
                    value = _get(item, dotted)
                    if column in row and value is not None and not _same(row[column], value):
                        item_issues.append(f"package_catalog_mismatch:{column}")
        items.append(item)
    ids = [i["item_id"] for i in items]
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    if dupes:
        raise CollectionError("duplicate_item_id", ", ".join(dupes[:5]))
    if catalog_rel:
        orphan = sorted(set(catalog_rows) - set(data_paths))
        if orphan:
            issues.append(f"package_catalog_rows_without_archive:{len(orphan)}")
    for it in items:
        issues += [f"{it['item_id']}:{x}" for x in it["issues"]]

    # Koleksiyon düzeyi CRS/kapsam: tüm raster varlıklar tek CRS'te ise kaydedilir; değilse "karışık" (uydurulmaz).
    rasters = [a["raster"] for it in items for a in it["assets"] if a.get("raster") and a["raster"].get("readable")]
    crs_keys = {(r["crs"].get("wkt_sha256"), r["crs"].get("epsg"), r["crs"].get("name")) for r in rasters if r.get("crs")}
    single = next(iter(crs_keys)) if len(crs_keys) == 1 else None
    extents = [r["extent"] for r in rasters if r.get("extent")]
    extent = {"minx": min(e["minx"] for e in extents), "miny": min(e["miny"] for e in extents),
              "maxx": max(e["maxx"] for e in extents), "maxy": max(e["maxy"] for e in extents)} if single and extents else None
    dates = sorted(d for d in (it["properties"].get("acquisition_date") for it in items) if d)
    boxes = [it["bbox_wgs84"] for it in items if it.get("bbox_wgs84")]

    # Kalite: kaynak durum olduğu gibi korunur; THERMALIS ekseni yalnız türetilir (yükseltilmez).
    qd = declaration["quality"]
    source_status = decl.resolve_ref(pkg.path, qd["source_status_ref"])
    status, rule = decl.quality_from_source_status(source_status)
    cross = [{"ref": ref, "value": decl.resolve_ref(pkg.path, ref)} for ref in qd.get("cross_check_refs", [])]
    conflicts = [c for c in cross if c["value"] is not None and str(c["value"]) != str(source_status)]
    limitations = decl.resolve_ref(pkg.path, qd["documented_limitations_ref"]) if qd.get("documented_limitations_ref") else None
    expected = decl.resolve_ref(pkg.path, col_decl["expected_item_count_ref"]) if col_decl.get("expected_item_count_ref") else None
    if conflicts:
        issues.append("source_quality_status_conflict:" + ",".join(c["ref"] for c in conflicts))
    if isinstance(expected, int) and expected != len(items):
        issues.append(f"item_count_differs_from_package_declaration:{len(items)}!={expected}")

    crs_units = next((r["crs"].get("units") for r in rasters if r.get("crs")), None) if single else None
    content_lines = "\n".join(f"{it['archive']['path']}|{it['archive']['sha256']}" for it in sorted(items, key=lambda i: i["archive"]["path"]))
    content_id = hashlib.sha256(content_lines.encode("utf-8")).hexdigest()
    documents = sorted([e.path for e in pkg.entries if journals[e.path]["format"] == "document"] + list(pkg.extra_files))
    layer_name = col_decl["layer_name"]
    asset_count = sum(len(it["assets"]) for it in items)
    raster_count = sum(1 for it in items for a in it["assets"] if a.get("raster"))
    checks = [
        {"id": "MAN-01", "title": "Manifest okundu; tüm dosyalar mevcut, boyutlar doğru, listede olmayan dosya yok",
         "status": "PASS", "detail": f"{len(pkg.entries)} dosya"},
        {"id": "DEC-01", "title": "Paket dışı beyan manifest SHA-256'sına bağlı", "status": "PASS", "detail": pkg.manifest_sha256},
        {"id": "FMT-01", "title": "Biçim ön denetimi", "status": "PASS", "detail": json.dumps(pf.formats, sort_keys=True)},
        {"id": "INT-01", "title": "Her dosya akış hâlinde SHA-256 ile manifestle doğrulandı (tek geçiş)", "status": "PASS",
         "detail": f"{len(journals)} dosya · {total} B"},
        {"id": "TAR-01", "title": "Arşiv üyeleri dizinlendi (konum + üye SHA-256)", "status": "PASS",
         "detail": f"{sum(1 for j in journals.values() if j['format'] == 'tar')} arşiv · {asset_count} üye"},
        {"id": "RST-01", "title": "Raster başlıkları okunabilir (kaynak değerler; dönüşüm yok)",
         "status": "PASS" if len(rasters) == raster_count else "FAIL", "detail": f"{len(rasters)}/{raster_count}"},
        {"id": "CAT-01", "title": "Paket sahne kataloğu ile çapraz denetim", "status": "PASS" if not any(
            "package_catalog" in x or "not_in_package_scene_catalog" in x for x in issues) else "WARN",
         "detail": catalog_rel or "katalog beyan edilmedi"},
        {"id": "QA-01", "title": "Kaynak QA durumu korundu; THERMALIS kalite ekseni türetildi", "status": "PASS",
         "detail": f"{source_status} → {status} ({rule})"},
    ]
    if any(c["status"] == "FAIL" for c in checks):
        raise CollectionError("collection_checks_failed", json.dumps([c for c in checks if c["status"] == "FAIL"], ensure_ascii=False)[:500])
    registered_at = _utc_now()
    src = declaration["source"]
    version_record = {
        "schema_version": "1.0", "record_kind": "raster_collection",
        "dataset_id": declaration["dataset_id"], "version": declaration["version"],
        "title": {"tr": None, "en": None, "label": " · ".join(declaration["dataset_id"].split("/")[1:])},
        "family": pkg.package_id.split("_", 1)[0],
        "source": {
            "package_id": pkg.package_id, "package_type": pkg.manifest_meta.get("family"),
            "package_built_at_utc": pkg.manifest_meta.get("created_utc") or pkg.manifest_meta.get("built_at_utc"),
            "package_status": pkg.manifest_meta.get("readiness_status"), "owner": src.get("organization"),
            "product": src.get("product"), "citation": src.get("citation"), "doi": src.get("doi"),
            "source_update_year": None, "origin_path": None, "ingested_from": str(pkg.root),
            "package_fingerprint": pkg.manifest_sha256,
            "temporal_coverage": {"first_acquisition": dates[0], "last_acquisition": dates[-1], "item_count": len(items),
                                  "basis": "item acquisition dates from source metadata", "continuous": None} if dates else None,
            "declaration": "records/THERMALIS_DECLARATION.json",
        },
        "access": {"classification": declaration["access_class"], "public_release": None, "authorization": None,
                   "policy": declaration.get("access_basis"),
                   "components": {"source_products": declaration["access_class"],
                                  "supplementary_records": declaration.get("supplementary_records")}},
        "storage": {"payload": "payload", "vector_sources": [], "raster_collection": "records/collection.json", "read_only": True,
                    "mode": "direct_publication_from_source_folder"},
        "crs": {"native_crs_name": single[2] if single else None, "native_wkt": wkts.get(single[0]) if single else None,
                "factory_code": single[1] if single else None, "unit": crs_units,
                "policy": "source native CRS preserved; no reprojection" + ("" if single else "; collection has multiple CRS"),
                "transformed": False},
        "layers": {layer_name: {
            "role": "raster_collection", "published": True, "geometry_type": "RasterCollection",
            "feature_count": len(items), "expected_feature_count": expected if isinstance(expected, int) else None,
            "crs_name": single[2] if single else None, "crs_matches_package_wkt": None,
            "extent_native": extent, "bbox_wgs84": [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes),
                                                    max(b[3] for b in boxes)] if boxes else None,
            "null_geometry": None, "empty_geometry": None, "source": None, "fields": [],
            "item_count": len(items), "asset_count": asset_count, "raster_asset_count": raster_count,
            "content": {"content_id": content_id, "algorithm": "sha256(sorted archive path|sha256 lines)"}}},
        "domains": [], "data_dictionary": [], "documents": documents, "derived_artifacts": [], "checks": checks,
        "registration": {"status": "REGISTERED", "ingest_id": ingest_id, "started_at": started, "registered_at": registered_at,
                         "tool": f"thermalis {__version__} ({TOOL})", "gdal": _gdal_version(),
                         "mode": "records_only_direct_publication"},
    }
    quality_record = {
        "schema_version": "1.0", "status": status, "source_status": source_status, "source_status_ref": qd["source_status_ref"],
        "source_status_mapping": rule, "source_status_cross_checks": cross,
        "documented_limitations": limitations, "documented_limitations_ref": qd.get("documented_limitations_ref"),
        "limitations_documents": qd.get("limitations_documents", []),
        "gates": [{"id": "SOURCE-QA", "status": str(source_status), "detail": qd["source_status_ref"]}],
        "gates_source": qd["source_status_ref"], "exceptions": [], "layer_status": [], "item_issues": issues,
        "note": "Kaynak QA durumu ve belgelenmiş sınırlamalar olduğu gibi korunur; kayıt analiz uygunluğu beyan etmez.",
    }
    integrity_record = {
        "schema_version": "1.0", "algorithm": "SHA-256", "scope": "PACKAGE/MANIFEST.json listesindeki tüm dosyalar (bayt bayt)",
        "files": [{"path": e.path, "bytes": e.bytes, "sha256": e.sha256} for e in pkg.entries],
        "file_count": len(pkg.entries), "total_bytes": total,
        "verifications": {"source_package": {"status": "PASS", "checked_files": len(journals), "problems": [],
                                             "method": "streaming_sha256_single_pass_with_member_index"}},
        "members": {"archives": sum(1 for j in journals.values() if j["format"] == "tar"), "members_hashed": asset_count},
    }
    collection_record = {
        "schema": COLLECTION_SCHEMA, "dataset_id": declaration["dataset_id"], "version": declaration["version"],
        "layer_name": layer_name, "interpreter": interpreter, "package_id": pkg.package_id, "manifest_sha256": pkg.manifest_sha256,
        "value_policy": "source values preserved; scale/offset recorded as declared in source metadata and never applied",
        "access_methods": {"tar_member": "/vsisubfile/<offset>_<bytes>,<archive>", "geotiff": "<file>"},
        "crs_wkt": wkts, "items": items, "issues": issues,
        "summary": {"items": len(items), "assets": asset_count, "raster_assets": raster_count,
                    "first_acquisition": dates[0] if dates else None, "last_acquisition": dates[-1] if dates else None},
    }
    records = {"version.json": canonical(version_record), "quality.json": canonical(quality_record),
               "integrity.json": canonical(integrity_record), "collection.json": canonical(collection_record),
               "THERMALIS_DECLARATION.json": canonical(declaration)}
    pinned.mkdir(parents=True, exist_ok=True)
    for name, data in records.items():
        tmp = pinned / f".{name}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, pinned / name)
    write_json_atomic(pinned / "binding.json", binding)
    return BuiltCollection(records, len(items), asset_count, raster_count, issues, False)


def _gdal_version() -> str | None:
    try:
        from osgeo import gdal

        return gdal.__version__
    except ImportError:  # pragma: no cover
        return None


def read_collection(folder: Path) -> dict[str, Any] | None:
    path = folder / "collection.json"
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return data if isinstance(data, dict) and data.get("schema") == COLLECTION_SCHEMA else None


__all__ = ["CollectionError", "SourcePackage", "load_source_package", "build_records", "index_file", "read_collection",
           "canonical"]
