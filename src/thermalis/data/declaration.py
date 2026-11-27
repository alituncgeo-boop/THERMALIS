"""Paket sözleşme eşlemesi: paketin DIŞINDA tutulan, manifest özetine bağlı kayıt beyanı.

Bazı kaynak paketleri (ör. HD02 Landsat) mevcut THERMALIS paket sözleşmesinin (``PACKAGE/RELEASE_STATUS.json``)
dosyalarını içermez. Orijinal paket DEĞİŞTİRİLMEZ; eksik kayıt kararları (veri kimliği, sürüm, erişim sınıfı,
kaynak atfı) veri sahibince bu beyanda verilir. Beyan ``PACKAGE/MANIFEST.json``'un SHA-256 özetine bağlıdır:
paket değişirse beyan geçersiz olur.

Konum: ``<veri kökü>/data/_declarations/<paket_id>.json`` (veya komut satırında açık yol).

Beyan bir yetki kaydı değildir. RESTRICTED/UNKNOWN veri için bulut yetkisi ayrıca gerekir (``THERMALIS_AUTHORIZATION.json``).
Bilimsel QA durumu beyanla YÜKSELTİLEMEZ: kalite, paketin kendi kaynak QA durumundan türetilir (bkz. :func:`quality_from_source_status`).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from .transfer_package import ACCESS_CLASSES, DATASET_ID_PATTERN, VERSION_PATTERN

SCHEMA = "thermalis.package-declaration/1"
DECLARATIONS_DIR = "_declarations"
_PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
KNOWN_INTERPRETERS = ("landsat_c2_l2",)
REQUIRED = ("schema", "package_id", "manifest_sha256", "dataset_id", "version", "access_class", "source", "quality",
            "declared_by", "declared_at")
TEMPORAL_SEMANTICS = ("observation_period", "source_acquisition_span", "reference_period", "validity_period", "unknown")


class DeclarationError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def declarations_dir(data_root: Path) -> Path:
    from .catalog import data_dir

    return data_dir(data_root) / DECLARATIONS_DIR


def declaration_path(data_root: Path, package_id: str) -> Path:
    if not _PACKAGE_ID.match(package_id or ""):
        raise DeclarationError("invalid_package_id", str(package_id)[:80])
    return declarations_dir(data_root) / f"{package_id}.json"


def validate(data: Any, *, manifest_sha256: str | None = None, package_id: str | None = None) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise DeclarationError("declaration_schema_invalid")
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        raise DeclarationError("declaration_fields_missing", ",".join(missing))
    if not _PACKAGE_ID.match(str(data["package_id"])):
        raise DeclarationError("invalid_package_id", str(data["package_id"])[:80])
    if not _HEX64.match(str(data["manifest_sha256"])):
        raise DeclarationError("declaration_manifest_sha256_invalid")
    if not DATASET_ID_PATTERN.match(str(data["dataset_id"])):
        raise DeclarationError("invalid_dataset_id", str(data["dataset_id"]))
    if not VERSION_PATTERN.match(str(data["version"])):
        raise DeclarationError("invalid_version", str(data["version"]))
    if data["access_class"] not in ACCESS_CLASSES:
        raise DeclarationError("declaration_access_class_invalid", str(data["access_class"]))
    src = data["source"]
    if not isinstance(src, dict) or not src.get("citation") or not src.get("product"):
        raise DeclarationError("declaration_source_incomplete", "source.product + source.citation required")
    q = data["quality"]
    if not isinstance(q, dict) or not q.get("source_status_ref"):
        raise DeclarationError("declaration_quality_ref_missing", "quality.source_status_ref required")
    if "status" in q:
        raise DeclarationError("declaration_cannot_set_quality_status", "quality is derived from the package, never declared")
    if ("collection" in data) == ("bundle" in data):
        raise DeclarationError("declaration_kind_ambiguous", "exactly one of 'collection' (raster collection) or 'bundle' (ZIP source bundle)")
    if "collection" in data:
        col = data["collection"]
        if not isinstance(col, dict):
            raise DeclarationError("declaration_collection_invalid")
        if col.get("interpreter") not in (None, *KNOWN_INTERPRETERS):
            raise DeclarationError("declaration_interpreter_unknown", str(col.get("interpreter")))
        if not re.match(r"^[A-Z][A-Z0-9_]{0,40}$", str(col.get("layer_name") or "")):
            raise DeclarationError("declaration_layer_name_invalid", str(col.get("layer_name")))
    else:
        b = data["bundle"]
        if not isinstance(b, dict) or not isinstance(b.get("tables", []), list) or not isinstance(b.get("roles", {}), dict):
            raise DeclarationError("declaration_bundle_invalid")
    t = data.get("temporal")
    if t is not None and (not isinstance(t, dict) or t.get("semantics") not in TEMPORAL_SEMANTICS):
        raise DeclarationError("declaration_temporal_semantics_invalid", str((t or {}).get("semantics") if isinstance(t, dict) else t))
    # Not: RESTRICTED/UNKNOWN erişim yetkisi beyanla VERİLMEZ; bulut yayını ayrıca geçerli THERMALIS_AUTHORIZATION.json ister.
    if manifest_sha256 is not None and data["manifest_sha256"] != manifest_sha256:
        raise DeclarationError("declaration_does_not_match_package", "manifest_sha256")
    if package_id is not None and data["package_id"] != package_id:
        raise DeclarationError("declaration_does_not_match_package", "package_id")
    return data


def load(path: Path, **expect: Any) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise DeclarationError("declaration_unreadable", type(exc).__name__) from None
    return validate(data, **expect)


def find(data_root: Path, package_id: str, manifest_sha256: str) -> dict[str, Any] | None:
    """Kayıtlı beyan (varsa ve pakete bağlıysa). Bağlı değilse :class:`DeclarationError`."""
    path = declaration_path(data_root, package_id)
    if not path.is_file():
        return None
    return load(path, manifest_sha256=manifest_sha256, package_id=package_id)


def save(data_root: Path, declaration: dict[str, Any]) -> Path:
    """Beyanı yalnız-oluştur olarak yazar; aynı içerik varsa dokunmaz, farklı içerikle üzerine yazmaz."""
    from ..storage import write_json_atomic

    validate(declaration)
    path = declaration_path(data_root, declaration["package_id"])
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing == declaration:
            return path
        raise DeclarationError("declaration_exists_with_different_content", str(path.name))
    write_json_atomic(path, declaration)
    return path


def resolve_ref(root: "Path | Callable[[str], Path]", ref: str) -> Any:
    """``PACKAGE/DOSYA.json#alan.alt_alan`` → değer (paket içinden, yalnız okuma). Yoksa ``None``.

    ``root``: paket kök klasörü veya göreli yolu gerçek dosyaya çeviren fonksiyon (alt küme görünümü).
    """
    rel, _, pointer = ref.partition("#")
    if ".." in rel.split("/") or rel.startswith("/"):
        return None
    path = root(rel) if callable(root) else root.joinpath(*rel.split("/"))
    if not path.is_file():
        return None
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    for part in [p for p in pointer.split(".") if p]:
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def quality_from_source_status(status: Any) -> tuple[str, str]:
    """Kaynak QA durumundan THERMALIS kalite ekseni; ASLA yükseltmez. (durum, uygulanan kural)

    - "FAIL" içeren → QA_FAIL
    - "CONDITIONAL" veya belgelenmiş sınırlama/koşul niteleyen geçiş ("LIMITATION", "_WITH_") → QA_CONDITIONAL
    - koşulsuz "PASS" → QA_PASS
    - belirsiz/boş → QA_PENDING
    """
    text = str(status or "").upper()
    if not text:
        return "QA_PENDING", "source_status_missing"
    if "FAIL" in text:
        return "QA_FAIL", "source_status_contains_fail"
    if "CONDITIONAL" in text or "LIMITATION" in text or "_WITH_" in text:
        return "QA_CONDITIONAL", "qualified_pass_maps_to_conditional"
    if "PASS" in text:
        return "QA_PASS", "unqualified_pass"
    return "QA_PENDING", "source_status_not_recognized"
