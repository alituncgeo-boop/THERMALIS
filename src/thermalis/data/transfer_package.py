"""THERMALIS kontrollü aktarım paketi (v1) okuyucusu.

Paket yapısı (HD01 ile tanımlanan sözleşme):

    <kök>/
      PACKAGE/MANIFEST.json                  dosya başına bytes + sha256
      PACKAGE/RELEASE_STATUS.json            dataset_id, version, status, core/auxiliary katmanlar
      PACKAGE/DATASET_INVENTORY_AND_SCHEMA.json
      PACKAGE/DATA_DICTIONARY.csv
      PACKAGE/CODED_DOMAINS.json
      PACKAGE/CRS_AND_UNITS.json
      PACKAGE/QA/*                           QA kayıtları, istisna kayıt defteri
      <veri klasörleri>/...                  kaynak veri (ör. File Geodatabase)

Bu modül yalnız okur; paketi değiştirmez.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .integrity import ManifestEntry

DATASET_ID_PATTERN = re.compile(r"^THERMALIS_DATA(/[A-Z0-9_]+){1,4}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$")
PACKAGE_DIR = "PACKAGE"
UNHASHED_PACKAGE_FILES = ("PACKAGE/MANIFEST.json", "PACKAGE/SHA256SUMS.csv")


class PackageError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def quality_status_from_release(status: str) -> str:
    """Paketin yayın durumundan kalite durumu çıkarılır; asla yükseltilmez."""
    upper = status.upper()
    if "QA_CONDITIONAL" in upper:
        return "QA_CONDITIONAL"
    if "QA_FAIL" in upper:
        return "QA_FAIL"
    if "QA_PASS" in upper:
        return "QA_PASS"
    return "UNVERIFIED"


@dataclass
class KnownNullIdentity:
    layer: str
    field: str
    records: list[dict[str, Any]]
    evidence: str

    @property
    def fids(self) -> list[int]:
        return sorted(int(r["OBJECTID"]) for r in self.records)


@dataclass
class TransferPackage:
    root: Path
    package_id: str
    dataset_id: str
    version: str
    release_status: dict[str, Any]
    manifest_meta: dict[str, Any]
    manifest: list[ManifestEntry]
    inventory: dict[str, Any]
    dictionary: list[dict[str, str]]
    domains: list[dict[str, Any]]
    crs: dict[str, Any]
    core_layers: list[str]
    auxiliary_layers: list[str]
    vector_sources: list[str]
    exception_register: list[dict[str, str]]
    exception_register_path: str | None
    gates: dict[str, Any] | None
    gates_path: str | None
    copy_verification: dict[str, Any] | None
    known_null_identities: list[KnownNullIdentity] = field(default_factory=list)

    @property
    def quality_status(self) -> str:
        return quality_status_from_release(str(self.release_status.get("status", "")))

    @property
    def family_prefix(self) -> str:
        return self.package_id.split("_", 1)[0]

    def expected_count(self, layer: str) -> int | None:
        entry = self.inventory.get(layer)
        if isinstance(entry, dict) and isinstance(entry.get("count"), int):
            return entry["count"]
        return None

    def dictionary_fields(self, layer: str) -> list[dict[str, str]]:
        return [row for row in self.dictionary if row.get("dataset") == layer]

    def documents(self) -> list[str]:
        """Veri dışı belgeler: PACKAGE/ altı ve .metadata klasörleri."""
        docs = [e.path for e in self.manifest if e.path.startswith(f"{PACKAGE_DIR}/") or ".metadata/" in e.path]
        docs += [p for p in UNHASHED_PACKAGE_FILES if (self.root / p).is_file()]
        return sorted(docs)


def _find_known_null_identities(root: Path, manifest: list[ManifestEntry]) -> list[KnownNullIdentity]:
    """Kanıt dosyalarındaki ``missing_identity_records`` beyanlarını bulur.

    Alan, aynı katmanın ``keys`` bölümünde boş sayısı kayıt sayısına eşit olan anahtardır.
    """
    found: list[KnownNullIdentity] = []
    for entry in manifest:
        if not entry.path.lower().endswith(".json") or "/EVIDENCE/" not in entry.path:
            continue
        try:
            data = _read_json(root / entry.path)
        except (OSError, ValueError):
            continue
        layers = data.get("layers") if isinstance(data, dict) else None
        if not isinstance(layers, dict):
            continue
        for layer_name, layer in layers.items():
            records = layer.get("missing_identity_records") if isinstance(layer, dict) else None
            if not records:
                continue
            keys = layer.get("keys", {})
            candidates = [k for k, v in keys.items() if isinstance(v, dict) and v.get("null_count") == len(records)]
            if len(candidates) == 1:
                found.append(KnownNullIdentity(layer_name, candidates[0], records, entry.path))
    return found


def load_package(root: Path) -> TransferPackage:
    root = root.resolve()
    pkg = root / PACKAGE_DIR
    if not pkg.is_dir():
        raise PackageError("package_dir_missing", str(pkg))
    if (root / "INCOMPLETE_DO_NOT_TRANSFER.txt").exists():
        raise PackageError("package_incomplete_marker")

    required = ["MANIFEST.json", "RELEASE_STATUS.json", "DATASET_INVENTORY_AND_SCHEMA.json",
                "DATA_DICTIONARY.csv", "CODED_DOMAINS.json", "CRS_AND_UNITS.json"]
    missing = [name for name in required if not (pkg / name).is_file()]
    if missing:
        raise PackageError("package_file_missing", ", ".join(missing))

    manifest_meta = _read_json(pkg / "MANIFEST.json")
    manifest = [ManifestEntry(r["path"], int(r["bytes"]), str(r["sha256"]).lower()) for r in manifest_meta["sha256_files"]]
    for entry in manifest:
        if entry.path.startswith("/") or ".." in entry.path.split("/") or "\\" in entry.path:
            raise PackageError("manifest_unsafe_path", entry.path)

    release = _read_json(pkg / "RELEASE_STATUS.json")
    dataset_id = str(release.get("dataset_id", ""))
    version = str(release.get("version", ""))
    if not DATASET_ID_PATTERN.match(dataset_id):
        raise PackageError("invalid_dataset_id", dataset_id)
    if not VERSION_PATTERN.match(version):
        raise PackageError("invalid_version", version)

    gdb_roots = sorted({e.path.split(".gdb/", 1)[0] + ".gdb" for e in manifest if ".gdb/" in e.path})

    register_path = next((e.path for e in manifest if e.path.startswith("PACKAGE/QA/") and e.path.endswith("EXCEPTION_REGISTER.csv")), None)
    gates_path = next((e.path for e in manifest if e.path.startswith("PACKAGE/QA/") and e.path.endswith("_GATES.json")), None)
    copy_verification_path = pkg / "SOURCE_COPY_VERIFICATION.json"

    return TransferPackage(
        root=root,
        package_id=str(manifest_meta.get("package_id", root.name)),
        dataset_id=dataset_id,
        version=version,
        release_status=release,
        manifest_meta={k: v for k, v in manifest_meta.items() if k != "sha256_files"},
        manifest=manifest,
        inventory=_read_json(pkg / "DATASET_INVENTORY_AND_SCHEMA.json"),
        dictionary=_read_csv(pkg / "DATA_DICTIONARY.csv"),
        domains=_read_json(pkg / "CODED_DOMAINS.json"),
        crs=_read_json(pkg / "CRS_AND_UNITS.json"),
        core_layers=list(release.get("core_dataset_names", [])),
        auxiliary_layers=list(release.get("auxiliary_dataset_names_not_for_primary_publication", [])),
        vector_sources=gdb_roots,
        exception_register=_read_csv(root / register_path) if register_path else [],
        exception_register_path=register_path,
        gates=_read_json(root / gates_path) if gates_path else None,
        gates_path=gates_path,
        copy_verification=_read_json(copy_verification_path) if copy_verification_path.is_file() else None,
        known_null_identities=_find_known_null_identities(root, manifest),
    )
