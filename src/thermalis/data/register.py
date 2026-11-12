"""Kontrollü aktarım paketinin DATA kataloğuna kaydı.

İlkeler:
- Kaynak paket yalnız okunur. Hiçbir onarım, dönüşüm, silme veya birleştirme yapılmaz.
- Kopya önce ``_staging`` altında hazırlanır; tüm denetimler geçmeden katalogda görünmez.
- Sürüm klasörü tek bir yeniden adlandırmayla yayımlanır; var olan sürüm üzerine yazılmaz.
- Yayından sonra bütünlük ve kayıt sayıları yeniden doğrulanır; ancak o zaman ``REGISTERED`` olur.
- Başarısız her deneme ``_failed`` altında raporuyla bırakılır.
"""

from __future__ import annotations

import gc
import os
import secrets
import time
import shutil
import stat
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from ..storage import read_json, write_json_atomic
from . import catalog
from .integrity import ManifestEntry, is_ephemeral, verify_tree
from .report import render_report
from .transfer_package import UNHASHED_PACKAGE_FILES, PackageError, TransferPackage, load_package
from .vector_inspect import gdal_available, inspect_vector, null_field_fids

PASS, FAIL, WARN, INFO = "PASS", "FAIL", "WARN", "INFO"


@dataclass
class Check:
    id: str
    title: str
    status: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "title": self.title, "status": self.status, "detail": self.detail}


@dataclass
class Outcome:
    status: str  # PASS | FAIL
    ingest_id: str
    dataset_id: str | None
    version: str | None
    location: Path | None
    report_path: Path | None
    checks: list[Check] = field(default_factory=list)
    error: str | None = None


class _Abort(Exception):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _copy_payload(pkg: TransferPackage, target: Path) -> int:
    paths = [e.path for e in pkg.manifest] + [p for p in UNHASHED_PACKAGE_FILES if (pkg.root / p).is_file()]
    for rel in paths:
        src = pkg.root / rel
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return len(paths)


def _make_read_only(folder: Path) -> None:
    for path in folder.rglob("*"):
        if path.is_file():
            os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)


def _make_writable(folder: Path) -> None:
    for path in folder.rglob("*"):
        if path.is_file():
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def _move_dir(source: Path, target: Path, attempts: int = 5) -> None:
    """Klasörü taşır. Windows'ta kısa süreli dosya kilitlerine (antivirüs taraması vb.) karşı birkaç kez dener."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            gc.collect()
            time.sleep(0.5 * (attempt + 1))


def _float_or_none(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


class _Run:
    def __init__(self, package_root: Path, data_root: Path, expect_status: str | None) -> None:
        self.package_root = package_root
        self.data_root = data_root
        self.expect_status = expect_status
        self.ingest_id = f"ing-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
        self.started_at = _utc_now()
        self.checks: list[Check] = []
        self.pkg: TransferPackage | None = None
        self.inspection: dict[str, Any] = {}
        self.layer_records: dict[str, Any] = {}
        self.gdal_version: str | None = None
        self.staging = catalog.data_dir(data_root) / "_staging" / self.ingest_id
        self.final: Path | None = None

    # -- yardımcılar -------------------------------------------------------------
    def check(self, cid: str, title: str, ok: bool | None, detail: str = "", fatal: bool = True, warn: bool = False) -> None:
        status = INFO if ok is None else (PASS if ok else (WARN if warn else FAIL))
        self.checks.append(Check(cid, title, status, detail))
        if status == FAIL and fatal:
            raise _Abort(f"{cid}: {detail}")

    # -- adımlar -----------------------------------------------------------------
    def load(self) -> None:
        try:
            self.pkg = load_package(self.package_root)
        except PackageError as exc:
            self.check("PKG-01", "Paket yapısı okunabilir", False, f"{exc.code} {exc.detail}".strip())
        pkg = self.pkg
        assert pkg is not None
        self.check("PKG-01", "Paket yapısı okunabilir", True, f"{pkg.package_id} · {len(pkg.manifest)} manifest dosyası")
        self.check("PKG-02", "Kalite durumu paketten okundu (yükseltilmez)", True, f"{pkg.release_status.get('status')} → {pkg.quality_status}")
        if self.expect_status:
            self.check("PKG-03", "Beklenen kalite durumu", pkg.quality_status == self.expect_status, f"beklenen {self.expect_status}, paket {pkg.quality_status}")
        ok, version = gdal_available()
        self.gdal_version = version if ok else None
        self.check("ENV-01", "GDAL kullanılabilir (File Geodatabase okuma)", ok, version)
        self.final = catalog.version_dir(self.data_root, pkg.dataset_id, pkg.version)
        exists = self.final.exists()
        self.check("CAT-01", "Bu sürüm katalogda yok (üzerine yazma engeli)", not exists,
                   f"{pkg.dataset_id} {pkg.version} zaten kayıtlı: {self.final}" if exists else str(self.final))

    def verify_source(self) -> None:
        pkg = self.pkg
        assert pkg is not None
        result = verify_tree(pkg.root, pkg.manifest, ignore=set(UNHASHED_PACKAGE_FILES))
        self.integrity_source = result.as_dict()
        self.check("INT-01", "Kaynak paket SHA-256 doğrulaması", result.passed,
                   f"{result.checked} dosya" + ("" if result.passed else f" · sorunlar: {result.problems[:10]}"))

    def copy(self) -> None:
        pkg = self.pkg
        assert pkg is not None
        payload = self.staging / "payload"
        payload.mkdir(parents=True, exist_ok=False)
        copied = _copy_payload(pkg, payload)
        result = verify_tree(payload, pkg.manifest, ignore=set(UNHASHED_PACKAGE_FILES))
        self.integrity_copy = result.as_dict()
        self.check("INT-02", "Kopya SHA-256 doğrulaması (staging)", result.passed,
                   f"{copied} dosya kopyalandı · {result.checked} doğrulandı" + ("" if result.passed else f" · {result.problems[:10]}"))

    def inspect(self) -> None:
        pkg = self.pkg
        assert pkg is not None
        payload = self.staging / "payload"
        self.check("VEC-00", "Vektör kaynak bulundu", bool(pkg.vector_sources), ", ".join(pkg.vector_sources) or "yok")
        for source in pkg.vector_sources:
            try:
                info = inspect_vector(payload / source, pkg.crs.get("native_wkt") or None)
            except Exception as exc:  # GDAL hataları
                self.check("VEC-01", f"{source} salt okunur açıldı", False, str(exc))
                return
            self.check("VEC-01", f"{source} salt okunur açıldı (OpenFileGDB)", True, f"{len(info['layers'])} katman")
            self.inspection[source] = info
            self._check_layers(source, info)
            self._check_domains(info)
            self._check_null_identities(payload / source, info)

    def _check_layers(self, source: str, info: dict[str, Any]) -> None:
        pkg = self.pkg
        assert pkg is not None
        layers = info["layers"]
        for name in pkg.core_layers:
            self.check("VEC-02", f"Çekirdek katman mevcut: {name}", name in layers, source)
        known = set(pkg.core_layers) | set(pkg.auxiliary_layers)
        unclassified = sorted(set(layers) - known)
        self.check("VEC-03", "Sınıflandırılmamış katman yok", not unclassified, ", ".join(unclassified) or "—", fatal=False, warn=True)

        expected_wkt = pkg.crs.get("native_wkt", "")
        for name, layer in layers.items():
            role = "core" if name in pkg.core_layers else ("auxiliary" if name in pkg.auxiliary_layers else "unclassified")
            expected = pkg.expected_count(name)
            counted = layer["feature_count"]
            self.check("VEC-04", f"Kayıt sayısı: {name}", expected is not None and counted == expected,
                       f"sayılan {counted} · beklenen {expected} · başlık {layer['header_feature_count']}",
                       fatal=role == "core", warn=role != "core")

            dictionary = [r for r in pkg.dictionary_fields(name) if r.get("type") not in ("OID", "Geometry")]
            gdal_fields = {f["name"] for f in layer["fields"]}
            missing = [r["field"] for r in dictionary if r["field"] not in gdal_fields]
            extra = sorted(gdal_fields - {r["field"] for r in dictionary})
            self.check("VEC-05", f"Öznitelik şeması sözlükle uyumlu: {name}", not missing,
                       f"eksik: {missing or '—'} · sözlükte olmayan: {extra or '—'}", fatal=role == "core", warn=role != "core")

            crs_ok = None
            if layer["geometry_type"] != "None":
                crs_ok = bool(layer["crs_matches_expected"]) if expected_wkt else False
                self.check("VEC-06", f"CRS paket WKT'si ile aynı (dönüşüm yok): {name}", crs_ok,
                           str(layer["crs_name"]), fatal=role == "core", warn=role != "core")

            dict_by_field = {r["field"]: r for r in pkg.dictionary_fields(name)}
            self.layer_records[name] = {
                "source": source,
                "role": role,
                "published": role == "core",
                "geometry_type": layer["geometry_type"],
                "feature_count": counted,
                "expected_feature_count": expected,
                "crs_name": layer["crs_name"],
                "crs_matches_package_wkt": crs_ok,
                "extent_native": layer["extent"],
                "null_geometry": layer["null_geometry"],
                "empty_geometry": layer["empty_geometry"],
                "fields": [
                    {**f, "dictionary_alias": dict_by_field.get(f["name"], {}).get("alias"),
                     "dictionary_type": dict_by_field.get(f["name"], {}).get("type"),
                     "coded_domain": dict_by_field.get(f["name"], {}).get("coded_domain") or None,
                     "null_count": layer["null_counts"].get(f["name"])}
                    for f in layer["fields"]
                ],
            }

    def _check_domains(self, info: dict[str, Any]) -> None:
        pkg = self.pkg
        assert pkg is not None
        actual = info["domains"]
        for domain in pkg.domains:
            name = domain["name"]
            got = actual.get(name, {}).get("coded_values")
            want = {str(k): v for k, v in domain.get("coded_values", {}).items()}
            self.check("DOM-01", f"Kodlu domain birebir korunmuş: {name}", got == want,
                       f"{len(want)} kod" if got == want else f"GDB: {len(got or {})} kod, paket: {len(want)} kod")

    def _check_null_identities(self, source_path: Path, info: dict[str, Any]) -> None:
        pkg = self.pkg
        assert pkg is not None
        for declared in pkg.known_null_identities:
            if declared.layer not in info["layers"]:
                continue
            actual = null_field_fids(source_path, declared.layer, declared.field)
            self.check("NUL-01", f"Kasıtlı boş {declared.layer}.{declared.field} kayıtları değişmemiş",
                       actual == declared.fids, f"GDB OBJECTID {actual} · beyan {declared.fids}")

    # -- kayıtlar ----------------------------------------------------------------
    def build_records(self) -> None:
        pkg = self.pkg
        assert pkg is not None
        rs = pkg.release_status
        self.version_record = {
            "schema_version": "1.0",
            "dataset_id": pkg.dataset_id,
            "version": pkg.version,
            "title": {"tr": None, "en": None, "label": " · ".join(pkg.dataset_id.split("/")[1:])},
            "family": pkg.family_prefix,
            "source": {
                "package_id": pkg.package_id,
                "package_type": pkg.manifest_meta.get("package_type"),
                "package_built_at_utc": pkg.manifest_meta.get("built_at_utc"),
                "package_status": rs.get("status"),
                "owner": rs.get("data_owner"),
                "source_update_year": rs.get("source_update_year"),
                "population_scope": rs.get("population_scope"),
                "origin_path": pkg.manifest_meta.get("source_path_on_origin_machine"),
                "ingested_from": str(pkg.root),
                "copy_verification": pkg.copy_verification,
            },
            "access": {
                "classification": "RESTRICTED" if rs.get("not_released_as_public_open_data") else "UNSPECIFIED",
                "public_release": False if rs.get("not_released_as_public_open_data") else None,
                "authorization": rs.get("transfer_authorization"),
                "policy": "Kaynak sahibinin kısıtlarına tabidir; açık veri olarak yayımlanamaz, onaylı kapsam dışında dağıtılamaz.",
            },
            "storage": {"payload": "payload", "vector_sources": [f"payload/{s}" for s in pkg.vector_sources], "read_only": True},
            "crs": {
                "native_crs_name": pkg.crs.get("native_crs_name"),
                "native_wkt": pkg.crs.get("native_wkt"),
                "factory_code": pkg.crs.get("factory_code"),
                "unit": pkg.crs.get("unit"),
                "policy": pkg.crs.get("policy"),
                "transformed": False,
            },
            "layers": self.layer_records,
            "domains": pkg.domains,
            "data_dictionary": pkg.dictionary,
            "documents": pkg.documents(),
            "derived_artifacts": [],
            "checks": [c.as_dict() for c in self.checks],
            "registration": {
                "status": "PENDING_FINAL_VERIFICATION",
                "ingest_id": self.ingest_id,
                "started_at": self.started_at,
                "registered_at": None,
                "tool": f"thermalis {__version__}",
                "gdal": self.gdal_version,
            },
        }
        self.quality_record = self._quality()
        self.integrity_record = {
            "schema_version": "1.0",
            "algorithm": "SHA-256",
            "scope": "payload/ altındaki manifest dosyaları (GDB .lock hariç)",
            "files": [{"path": e.path, "bytes": e.bytes, "sha256": e.sha256} for e in pkg.manifest],
            "file_count": len(pkg.manifest),
            "total_bytes": sum(e.bytes for e in pkg.manifest),
            "verifications": {"source_package": self.integrity_source, "staging_copy": self.integrity_copy},
        }

    def _quality(self) -> dict[str, Any]:
        pkg = self.pkg
        assert pkg is not None
        exceptions: list[dict[str, Any]] = []
        for row in pkg.exception_register:
            exceptions.append(
                {
                    "exception_id": row.get("exception_id"),
                    "category": row.get("category"),
                    "dataset": row.get("dataset"),
                    "feature_a": row.get("feature_a") or None,
                    "oid_a": _int_or_none(row.get("oid_a")),
                    "feature_b": row.get("feature_b") or None,
                    "oid_b": _int_or_none(row.get("oid_b")),
                    "area_m2": _float_or_none(row.get("area_m2")),
                    "percent_of_feature": _float_or_none(row.get("percent_of_feature")),
                    "evidence": row.get("evidence"),
                    "qa_action": row.get("qa_action"),
                    "disposition": row.get("disposition"),
                    "source": pkg.exception_register_path,
                }
            )
        for declared in pkg.known_null_identities:
            for n, record in enumerate(sorted(declared.records, key=lambda r: int(r["OBJECTID"])), start=1):
                exceptions.append(
                    {
                        "exception_id": f"{pkg.family_prefix}-NULLID-{n:02d}",
                        "category": "INTENTIONAL_NULL_IDENTIFIER",
                        "dataset": declared.layer,
                        "feature_a": record.get("AD"),
                        "oid_a": int(record["OBJECTID"]),
                        "feature_b": None,
                        "oid_b": None,
                        "area_m2": None,
                        "percent_of_feature": None,
                        "evidence": f"{declared.field} NULL (orman/askerî özel alan); ILCEKN={record.get('ILCEKN')}",
                        "qa_action": "Değiştirme, doldurma veya silme yapılmaz",
                        "disposition": "DOCUMENTED_INTENTIONAL",
                        "source": declared.evidence,
                    }
                )
        aux_states = (pkg.gates or {}).get("auxiliary", {})
        for n, name in enumerate(pkg.auxiliary_layers, start=1):
            layer = self.layer_records.get(name, {})
            state = aux_states.get(name, "NOT_ASSESSED_IN_PACKAGE")
            exceptions.append(
                {
                    "exception_id": f"{pkg.family_prefix}-AUX-{n:02d}",
                    "category": "AUXILIARY_LAYER_STATUS",
                    "dataset": name,
                    "feature_a": None, "oid_a": None, "feature_b": None, "oid_b": None,
                    "area_m2": None, "percent_of_feature": None,
                    "evidence": f"geometri durumu: {state}; tür: {layer.get('geometry_type')}; kayıt: {layer.get('feature_count')}",
                    "qa_action": "Üretim idari sınırı olarak yayımlanmaz; köken için saklanır",
                    "disposition": "AUXILIARY_NOT_PUBLISHED",
                    "source": pkg.gates_path,
                }
            )
        return {
            "schema_version": "1.0",
            "dataset_id": pkg.dataset_id,
            "version": pkg.version,
            "status": pkg.quality_status,
            "status_source": f"PACKAGE/RELEASE_STATUS.json: {pkg.release_status.get('status')}",
            "upgrade_policy": "Durum otomatik yükseltilmez; bilimsel PASS yalnız belgelenmiş veri sorumlusu kararıyla verilebilir.",
            "steward_disposition": pkg.release_status.get("steward_topology_disposition"),
            "fitness_for_use": "Mekânsal birleştirme/çakıştırma yapan her analiz, çakışma, şerit ve kapsama boşluklarını nasıl ele aldığını açıkça beyan etmelidir.",
            "gates": (pkg.gates or {}).get("gates", []),
            "gates_source": pkg.gates_path,
            "summary": dict(Counter(e["category"] for e in exceptions)),
            "exceptions": exceptions,
        }

    # -- yayın -------------------------------------------------------------------
    def publish(self) -> None:
        pkg = self.pkg
        assert pkg is not None and self.final is not None
        write_json_atomic(self.staging / "version.json", self.version_record)
        write_json_atomic(self.staging / "quality.json", self.quality_record)
        write_json_atomic(self.staging / "integrity.json", self.integrity_record)
        _make_read_only(self.staging / "payload")
        self.final.parent.mkdir(parents=True, exist_ok=True)
        if self.final.exists():
            self.check("CAT-02", "Yayın sırasında sürüm hâlâ boş", False, str(self.final))
        _move_dir(self.staging, self.final)
        self.check("CAT-02", "Sürüm klasörü tek adımda yayımlandı", True, str(self.final))

    def verify_final(self) -> None:
        pkg = self.pkg
        assert pkg is not None and self.final is not None
        payload = self.final / "payload"
        result = verify_tree(payload, pkg.manifest, ignore=set(UNHASHED_PACKAGE_FILES))
        self.integrity_record["verifications"]["final_location"] = result.as_dict()
        self.check("INT-03", "Kayıt sonrası SHA-256 doğrulaması (nihai konum)", result.passed,
                   f"{result.checked} dosya" + ("" if result.passed else f" · {result.problems[:10]}"))
        for source in pkg.vector_sources:
            info = inspect_vector(payload / source)
            for name in pkg.core_layers:
                counted = info["layers"][name]["feature_count"] if name in info["layers"] else None
                self.check("VEC-07", f"Kayıt sonrası sayım: {name}", counted == pkg.expected_count(name),
                           f"sayılan {counted} · beklenen {pkg.expected_count(name)}")
        leftovers = [p.name for p in payload.rglob("*") if p.is_file() and is_ephemeral(p)]
        self.check("INT-04", "Doğrulama kaynağa kilit/geçici dosya bırakmadı", not leftovers, ", ".join(leftovers) or "—")

    def finalize(self) -> None:
        pkg = self.pkg
        assert pkg is not None and self.final is not None
        self.version_record["checks"] = [c.as_dict() for c in self.checks]
        self.version_record["registration"]["status"] = "REGISTERED"
        self.version_record["registration"]["registered_at"] = _utc_now()
        write_json_atomic(self.final / "integrity.json", self.integrity_record)
        write_json_atomic(self.final / "version.json", self.version_record)
        dataset_file = self.final.parent / "dataset.json"
        dataset = read_json(dataset_file) if dataset_file.is_file() else {
            "schema_version": "1.0", "dataset_id": pkg.dataset_id, "family": pkg.family_prefix, "versions": []}
        dataset["versions"] = sorted(set(dataset["versions"]) | {pkg.version})
        dataset["latest_registered"] = pkg.version
        write_json_atomic(dataset_file, dataset)


def register_package(package_root: Path, data_root: Path, expect_status: str | None = None) -> Outcome:
    run = _Run(Path(package_root), Path(data_root), expect_status)
    error: str | None = None
    published = False
    try:
        run.load()
        run.verify_source()
        run.copy()
        run.inspect()
        run.build_records()
        run.publish()
        published = True
        run.verify_final()
        run.finalize()
    except _Abort as exc:
        error = str(exc)
    except Exception as exc:  # beklenmeyen hata da yarım kayıt bırakmaz
        error = f"UNEXPECTED: {type(exc).__name__}: {exc}"
        run.checks.append(Check("SYS-01", "Beklenmeyen hata", FAIL, error))

    pkg = run.pkg
    status = FAIL if error else PASS
    location: Path | None
    if status == PASS:
        location = run.final
    else:
        location = catalog.data_dir(run.data_root) / "_failed" / run.ingest_id
        location.parent.mkdir(parents=True, exist_ok=True)
        source = run.final if published and run.final is not None and run.final.exists() else run.staging
        if source.exists():
            _make_writable(source)
            _move_dir(source, location)
            vr = location / "version.json"
            if vr.is_file():
                record = read_json(vr)
                record["registration"]["status"] = "FAILED"
                write_json_atomic(vr, record)
        else:
            location.mkdir(parents=True, exist_ok=True)

    report_name = f"{pkg.family_prefix if pkg else 'DATA'}_DATA_INGESTION_REPORT.md"
    report_path = (location or run.staging) / report_name
    report_path.write_text(
        render_report(run=run, status=status, error=error, location=location),
        encoding="utf-8",
        newline="\n",
    )
    return Outcome(
        status=status,
        ingest_id=run.ingest_id,
        dataset_id=pkg.dataset_id if pkg else None,
        version=pkg.version if pkg else None,
        location=location,
        report_path=report_path,
        checks=run.checks,
        error=error,
    )


def verify_registered(data_root: Path, dataset_id: str, version: str) -> list[Check]:
    """Kayıtlı bir sürümün bütünlüğünü ve çekirdek sayımlarını yeniden doğrular (yalnız okuma)."""
    folder = catalog.version_dir(data_root, dataset_id, version)
    record = read_json(folder / "version.json")
    integrity = read_json(folder / "integrity.json")
    entries = [ManifestEntry(f["path"], f["bytes"], f["sha256"]) for f in integrity["files"]]
    result = verify_tree(folder / "payload", entries, ignore=set(UNHASHED_PACKAGE_FILES))
    checks = [Check("INT-03", "SHA-256 doğrulaması", PASS if result.passed else FAIL, f"{result.checked} dosya {result.problems[:10]}")]
    for source in record["storage"]["vector_sources"]:
        info = inspect_vector(folder / source)
        for name, layer in record["layers"].items():
            if layer["role"] != "core":
                continue
            counted = info["layers"].get(name, {}).get("feature_count")
            checks.append(Check("VEC-07", f"Sayım: {name}", PASS if counted == layer["expected_feature_count"] else FAIL,
                                f"sayılan {counted} · beklenen {layer['expected_feature_count']}"))
    return checks
