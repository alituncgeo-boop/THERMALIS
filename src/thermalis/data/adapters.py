"""Veri formatı adaptörleri: hangi veri türlerinin gerçekten desteklendiğini tek yerde tutar.

Kayıt defterinde yalnız **uygulanmış ve gerçek veriyle doğrulanmış** adaptörler bulunur.
Şu an: File Geodatabase (vektör), HD01 ile doğrulandı.

Tanıma dosya adına değil manifeste dayanır: yalnız ``MANIFEST.json`` içinde SHA-256'sı
verilmiş dosyalar sınıflandırılır. ``PACKAGE/`` ve ``*.metadata/`` altı belge sayılır.
Hiçbir adaptörün tanımadığı veri dosyası olan paket işlenmez: ``data ingest`` onu
``BLOCKED`` (``format_not_supported``) olarak durdurur ve kaynak paketi salt okunur
inceleyip bir *destek eksikliği raporu* (``SUPPORT_GAP.json``) yazar. Varsayımla kabul yoktur.

Yeni bir veri türü geldiğinde (ayrı veri havuzu veya paralel sistem kurulmaz):
1. ``SUPPORT_GAP.json`` ve kaynak paket salt okunur incelenir.
2. Bu modüle bir ``FormatAdapter`` eklenir; okuma/doğrulama kodu ``data/`` altında yazılır
   ve ``register.py`` inceleme adımına bağlanır (bugün ``vector_inspect`` nasıl bağlıysa).
3. Metadata, anlamsal sözlük ve bilimsel QA kuralları pakette tanımlanır ve denetlenir.
4. Sentetik testler + gerçek veri üzerinde deneme; ancak ondan sonra ``ADAPTERS`` listesine girer.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .transfer_package import TransferPackage


@dataclass(frozen=True)
class FormatAdapter:
    key: str
    label: str
    suffixes: tuple[str, ...]
    container: bool  # True: klasör biçimli kaynak (ör. .gdb); üyeleri tek kaynak sayılır
    checks: tuple[str, ...]
    validated_with: str


ADAPTERS: tuple[FormatAdapter, ...] = (
    FormatAdapter(
        "filegdb_vector", "Esri File Geodatabase (vektör)", (".gdb",), True,
        ("salt okunur açılış (OpenFileGDB)", "katmanların çekirdek/yardımcı sınıflaması",
         "kayıt sayısı (tek tek okunarak) = beyan", "alan şeması = veri sözlüğü",
         "katman CRS'si = paket WKT'si (dönüşüm yok)", "kodlu domainler birebir",
         "kasıtlı boş kimlik kayıtları = beyan", "katman kapsamı (yerel CRS)"),
        "HD01_ADMIN_BOUNDARIES 1.0.0-rc1",
    ),
)
DOCUMENT_PREFIXES = ("PACKAGE/",)
DOCUMENT_MARKERS = (".metadata/",)


@dataclass
class FormatPlan:
    sources: dict[str, list[str]] = field(default_factory=dict)  # adaptör anahtarı -> kaynak yolları
    documents: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)  # hiçbir adaptörün tanımadığı veri dosyaları
    declared_kinds: list[str] = field(default_factory=list)  # paket metadata'sında beyan edilen ama desteklenmeyen türler

    @property
    def supported(self) -> bool:
        return bool(self.sources) and not self.unsupported and not self.declared_kinds

    def as_dict(self) -> dict:
        return {"sources": self.sources, "unsupported_files": self.unsupported,
                "unsupported_declared_kinds": self.declared_kinds, "document_count": len(self.documents)}


def _is_document(path: str) -> bool:
    return path.startswith(DOCUMENT_PREFIXES) or any(m in path for m in DOCUMENT_MARKERS)


def classify_paths(paths: list[str], declared_kinds: list[str] | None = None) -> FormatPlan:
    plan = FormatPlan()
    for path in sorted(paths):
        if _is_document(path):
            plan.documents.append(path)
            continue
        lower = path.lower()
        for adapter in ADAPTERS:
            hit = next((s for s in adapter.suffixes if (f"{s}/" in lower if adapter.container else lower.endswith(s))), None)
            if hit:
                root = path[: lower.index(f"{hit}/") + len(hit)] if adapter.container else path
                bucket = plan.sources.setdefault(adapter.key, [])
                if root not in bucket:
                    bucket.append(root)
                break
        else:
            plan.unsupported.append(path)
    known = {a.key for a in ADAPTERS}
    plan.declared_kinds = [k for k in declared_kinds or [] if k not in known]
    return plan


def classify_package(pkg: "TransferPackage") -> FormatPlan:
    kinds = pkg.release_status.get("data_kinds")
    return classify_paths([e.path for e in pkg.manifest], kinds if isinstance(kinds, list) else None)


def support_gap(pkg: "TransferPackage", plan: FormatPlan) -> dict:
    """Desteklenmeyen paketin salt okunur özeti: yeni adaptör geliştirmek için başlangıç kanıtı.

    Yalnız manifest ve paket metadata'sı okunur; veri dosyaları açılmaz, anlamları tahmin edilmez.
    """
    sizes = {e.path: e.bytes for e in pkg.manifest}
    by_suffix: dict[str, dict] = defaultdict(lambda: {"files": 0, "bytes": 0, "examples": []})
    for path in plan.unsupported:
        suffix = PurePosixPath(path).suffix.lower() or "(uzantısız)"
        entry = by_suffix[suffix]
        entry["files"] += 1
        entry["bytes"] += sizes.get(path, 0)
        if len(entry["examples"]) < 5:
            entry["examples"].append(path)
    present = {name: (pkg.root / "PACKAGE" / name).is_file() for name in (
        "RELEASE_STATUS.json", "DATASET_INVENTORY_AND_SCHEMA.json", "DATA_DICTIONARY.csv",
        "CODED_DOMAINS.json", "CRS_AND_UNITS.json", "PROVENANCE_AND_LICENSE.md")}
    return {
        "schema_version": "1.0",
        "dataset_id": pkg.dataset_id, "version": pkg.version, "package_id": pkg.package_id,
        "supported_adapters": [{"key": a.key, "label": a.label, "validated_with": a.validated_with} for a in ADAPTERS],
        "recognised_sources": plan.sources,
        "unsupported_by_suffix": dict(sorted(by_suffix.items())),
        "unsupported_declared_kinds": plan.declared_kinds,
        "package_metadata_files": present,
        "release_status_keys": sorted(pkg.release_status),
        "next_steps": [
            "Kaynak paketi salt okunur incele; format ve ürün tanımını veri sahibinin belgesinden doğrula.",
            "data/adapters.py'ye FormatAdapter ekle; okuma/doğrulama kodunu register.py inceleme adımına bağla.",
            "Metadata, anlamsal sözlük ve bilimsel QA kurallarını tanımla; bilinmeyen anlamları varsayma.",
            "Sentetik testler ve gerçek veriyle dene; doğrulandıktan sonra adaptörü kalıcı olarak ekle.",
        ],
    }


def support_matrix() -> list[dict]:
    return [{"key": a.key, "label": a.label, "suffixes": list(a.suffixes), "checks": list(a.checks),
             "validated_with": a.validated_with} for a in ADAPTERS]
