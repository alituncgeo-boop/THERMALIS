"""Manifest-bound preservation of a heterogeneous source directory."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat

from ..objectstore.errors import InvalidKey
from ..objectstore.keys import validate_key
from ..storage import write_json_atomic
from . import declaration as decl
from .collection import BuiltCollection, CollectionError, SourcePackage, canonical, index_file
from .transfer_package import ManifestEntry

FORMAT = "thermalis-directory-manifest-v1"
KIND = "directory_source"
MANIFEST = "MANIFEST.json"
ANCHOR = "MANIFEST_SHA256.txt"
LAYER = "SOURCE_FILES"
_SHA256 = re.compile(r"[a-fA-F0-9]{64}")


def is_directory_source(root: Path) -> bool:
    # A legacy raster collection and a ZIP bundle take precedence in the shared loader.
    return (root / MANIFEST).is_file()


def _inventory(root: Path) -> tuple[dict[str, int], set[str]]:
    """Walk regular entries without traversing links or Windows junctions."""
    files, directories, seen = {}, set(), set()

    def visit(folder):
        for path in sorted(folder.iterdir()):
            relative = path.relative_to(root).as_posix()
            validate_key(relative)
            fold = relative.casefold()
            if fold in seen:
                raise CollectionError("directory_path_case_collision", relative)
            seen.add(fold)
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise CollectionError("directory_link_unsupported", relative)
            if stat.S_ISDIR(info.st_mode):
                directories.add(relative)
                visit(path)
            elif stat.S_ISREG(info.st_mode):
                files[relative] = info.st_size
            else:
                raise CollectionError("directory_special_file_unsupported", relative)

    if root.is_symlink() or root.is_junction():
        raise CollectionError("directory_link_unsupported")
    try:
        visit(root)
    except InvalidKey as exc:
        raise CollectionError("manifest_unsafe_path", exc.key or "") from None
    return files, directories


def load_directory_source(root: Path) -> SourcePackage:
    root = Path(os.path.abspath(root))
    try:
        files, directories = _inventory(root)
        if MANIFEST not in files or ANCHOR not in files:
            raise CollectionError("directory_manifest_controls_missing")
        raw = (root / MANIFEST).read_bytes()
        anchor = (root / ANCHOR).read_bytes()
        meta = json.loads(raw.decode("utf-8-sig"))
        digest = hashlib.sha256(raw).hexdigest()
        fields = anchor.decode("utf-8-sig").split()
    except (OSError, ValueError) as exc:
        raise CollectionError("directory_manifest_unreadable", type(exc).__name__) from None
    if not isinstance(meta, dict) or meta.get("format") != FORMAT:
        raise CollectionError("directory_manifest_format_invalid")
    if not fields or fields[0].lower() != digest or fields[1:] not in ([], [MANIFEST], ["*" + MANIFEST]):
        raise CollectionError("directory_manifest_anchor_mismatch")
    rows = meta.get("files")
    if not isinstance(rows, list) or not rows:
        raise CollectionError("manifest_file_list_missing", "files")
    entries, seen = [], {MANIFEST.casefold(), ANCHOR.casefold()}
    for row in rows:
        try:
            name, size, sha = row["path"], row["bytes"], row["sha256"]
            validate_key(name)
            if type(size) is not int or size < 0 or not isinstance(sha, str) or not _SHA256.fullmatch(sha):
                raise ValueError
        except (KeyError, TypeError, ValueError, InvalidKey):
            raise CollectionError("manifest_entry_invalid") from None
        if name.casefold() in seen:
            raise CollectionError("manifest_duplicate_path", name)
        seen.add(name.casefold())
        if name not in files:
            raise CollectionError("package_file_missing", name)
        if files[name] != size:
            raise CollectionError("package_file_size_mismatch", name)
        entries.append(ManifestEntry(name, size, sha.lower()))
    if set(files) != {MANIFEST, ANCHOR, *(e.path for e in entries)}:
        raise CollectionError("package_file_not_in_manifest")
    declared = meta.get("source_directories", [])
    if (not isinstance(declared, list) or any(not isinstance(rel, str) or rel not in directories for rel in declared)
            or len(set(declared)) != len(declared)):
        raise CollectionError("directory_manifest_directory_mismatch")
    streams = meta.get("source_ntfs_streams")
    if streams is not None and (not isinstance(streams, str) or not any(e.path == streams for e in entries)):
        raise CollectionError("directory_manifest_stream_inventory_missing")
    entries.extend((ManifestEntry(MANIFEST, len(raw), digest),
                    ManifestEntry(ANCHOR, len(anchor), hashlib.sha256(anchor).hexdigest())))
    return SourcePackage(root, meta.get("release_id") or meta.get("package_id") or root.name, digest, {
        "artifact_kind": meta.get("artifact_kind"), "directories": sorted(directories),
        "named_stream_inventory": streams,
    }, sorted(entries, key=lambda entry: entry.path), kind=KIND)


def build_directory_records(pkg, declaration, work, *, ingest_id, cancel=None, progress=None, log=None):
    decl.validate(declaration, manifest_sha256=pkg.manifest_sha256, package_id=pkg.package_id)
    spec = declaration.get("directory")
    if not isinstance(spec, dict):
        raise CollectionError("declaration_directory_section_missing")
    by_path = {entry.path: entry for entry in pkg.entries}
    done = 0

    def tick(size):
        nonlocal done
        done += size
        if progress is not None:
            progress("index", done, total)

    total = pkg.total_bytes
    for entry in pkg.entries:
        if cancel is not None:
            cancel()
        index_file(pkg, entry, work, interpreter=None, cancel=cancel, progress=tick, inspect_science=False)

    # Restrict metadata pointers to verified files in this package.
    def resolve(ref):
        path = ref.partition("#")[0]
        if path not in by_path:
            return None
        return decl.resolve_ref(pkg.path, ref)

    qd, src = declaration["quality"], declaration["source"]
    source_status = resolve(qd["source_status_ref"])
    quality, rule = decl.quality_from_source_status(source_status)
    cross = [{"ref": ref, "value": resolve(ref)} for ref in qd.get("cross_check_refs", [])]
    issues = ["source_quality_status_not_found"] if source_status is None else []
    issues.extend("source_quality_status_conflict:" + x["ref"] for x in cross
                  if x["value"] is not None and x["value"] != source_status)
    docs = spec.get("documents", [])
    if any(path not in by_path or by_path[path].bytes > 2 * 1024 * 1024 for path in docs):
        raise CollectionError("directory_document_not_small_manifest_entry")
    documents = sorted(set([MANIFEST, ANCHOR, *docs]))
    temporal = declaration.get("temporal")
    if temporal:
        temporal = {"semantics": temporal["semantics"], "start": resolve(temporal.get("start_ref", "")),
                    "end": resolve(temporal.get("end_ref", "")), "time_reference": temporal.get("time_reference")}
    fingerprint = {"schema": "thermalis.directory-record-binding/1", "manifest_sha256": pkg.manifest_sha256,
                   "declaration_sha256": hashlib.sha256(canonical(declaration)).hexdigest(),
                   "directories": pkg.manifest_meta["directories"],
                   "anchor_sha256": by_path[ANCHOR].sha256}
    pinned = work / "directory-records.json"
    if pinned.is_file():
        saved = json.loads(pinned.read_text(encoding="utf-8"))
        if saved["binding"] == fingerprint:
            return BuiltCollection({name: canonical(value) for name, value in saved["records"].items()},
                                   1, len(pkg.entries), 0, issues, True)
    files = [{"path": e.path, "bytes": e.bytes, "sha256": e.sha256} for e in pkg.entries]
    content_id = hashlib.sha256(canonical({"files": files, "directories": pkg.manifest_meta["directories"]})).hexdigest()
    version = {
        "schema_version": "1.0", "record_kind": KIND, "dataset_id": declaration["dataset_id"], "version": declaration["version"],
        "title": {"label": " · ".join(declaration["dataset_id"].split("/")[1:])}, "family": pkg.package_id.split("_", 1)[0],
        "source": {"package_id": pkg.package_id, "package_type": pkg.manifest_meta["artifact_kind"], "owner": src.get("organization"),
                   "product": src["product"], "citation": src["citation"], "package_status": source_status,
                   "package_fingerprint": pkg.manifest_sha256, "fingerprint_basis": "sha256(MANIFEST.json)",
                   "temporal_coverage": temporal, "declaration": "records/THERMALIS_DECLARATION.json"},
        "access": {"classification": declaration["access_class"], "public_release": None,
                   "authorization": declaration.get("authorization_reference"), "policy": declaration.get("access_basis"),
                   "components": {"source_products": declaration["access_class"],
                                  "supplementary_records": declaration.get("supplementary_records")}},
        "storage": {"payload": "payload", "read_only": True, "mode": "direct_publication_from_source_folder"},
        "crs": {"transformed": False, "policy": "native formats preserved; dataset-wide CRS not inferred"},
        "layers": {LAYER: {"source": MANIFEST, "role": "source_directory", "published": True, "fields": [],
                           "content": {"content_id": content_id, "algorithm": "sha256_directory_inventory_v1"}}},
        "documents": documents, "registration": {"status": "REGISTERED", "ingest_id": ingest_id,
                                                   "registered_at": declaration["declared_at"], "mode": "records_only_direct_publication"},
    }
    records = {
        "version.json": version,
        "quality.json": {"schema_version": "1.0", "status": quality, "source_status": source_status,
                         "source_status_ref": qd["source_status_ref"], "source_status_mapping": rule, "source_status_cross_checks": cross,
                         "documented_limitations": resolve(qd.get("documented_limitations_ref", "")),
                         "limitations_documents": qd.get("limitations_documents", []), "item_issues": issues},
        "integrity.json": {"schema_version": "1.0", "algorithm": "SHA-256", "files": files,
                           "file_count": len(files), "total_bytes": total,
                           "verifications": {"source_package": {"status": "PASS", "checked_files": len(files),
                                                                 "method": "streaming_sha256", "problems": []}}},
        "directory.json": {"schema": "thermalis.directory-source/1", "manifest": MANIFEST, "manifest_sha256": pkg.manifest_sha256,
                           "directories": pkg.manifest_meta["directories"], "file_count": len(files),
                           "analysis_ready": False, "named_stream_inventory": pkg.manifest_meta["named_stream_inventory"],
                           "named_streams_transferred": False, "byte_scope": "regular file streams"},
        "THERMALIS_DECLARATION.json": declaration,
    }
    write_json_atomic(pinned, {"binding": fingerprint, "records": records})
    return BuiltCollection({name: canonical(value) for name, value in records.items()}, 1, len(files), 0, issues, False)
