"""Otomatik entegrasyon iş akışı: keşif, kapılar, idempotentlik, kurtarma, dizin, destek eksikliği."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import build_package, write_manifest
from thermalis.data import catalog
from thermalis.data.adapters import classify_paths, support_matrix
from thermalis.data.catalog_index import (
    finding_summary,
    query_assets,
    query_completeness,
    query_findings,
    query_layers,
    query_record_issues,
    query_relations,
    query_versions,
    rebuild_index,
)
from thermalis.data.ingest import IngestLock, IngestLocked, discover, release_stale_lock, run_ingest
from thermalis.data.integrity import sha256_file
from thermalis.data.register import register_package

DATASET = "THERMALIS_DATA/TT01_ADMIN_BOUNDARIES/ISTANBUL"


@pytest.fixture()
def incoming(tmp_path: Path) -> Path:
    folder = tmp_path / "INCOMING"
    folder.mkdir()
    return folder


def _tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in root.rglob("*") if p.is_file()}


def _rewrite(package: Path, rel: str, text: str) -> None:
    (package / rel).write_text(text, encoding="utf-8")
    write_manifest(package)


# ----------------------------------------------------------------------------- başarılı akış

def test_ingest_success_end_to_end(incoming: Path, data_root: Path) -> None:
    package = build_package(incoming / "TT01_v1")
    before = _tree(package)
    summary = run_ingest(incoming, data_root, settle_seconds=0)

    assert [c.state for c in summary.candidates] == ["READY"]
    (r,) = summary.results
    assert (r.technical, r.quality, r.audit, r.access) == ("PASS", "QA_CONDITIONAL", "NOT_CONFIGURED", "RESTRICTED")
    stages = {s.id: s.status for s in r.stages.values()}
    assert "NOT_RUN" not in stages.values(), stages
    assert stages["S08"] == "WARN" and stages["S09"] == "WARN"  # anlamsal tanım ve lisans belgesi yok: dürüstçe uyarı
    assert stages["S16"] == "SKIPPED" and stages["S17"] == "PASS"
    assert r.source_snapshot_unchanged is True and r.prior_versions_unchanged is True
    assert _tree(package) == before, "kaynak paket değişmemeli"

    result = json.loads((r.folder / "INGESTION_RESULT.json").read_text(encoding="utf-8"))
    assert result["status"] == {"technical": "PASS", "quality": "QA_CONDITIONAL", "independent_audit": "NOT_CONFIGURED", "access": "RESTRICTED"}
    assert Path(result["locations"]["integrity_report"]).is_file() and Path(result["locations"]["quality_report"]).is_file()
    report = (r.folder / "INGESTION_REPORT.md").read_text(encoding="utf-8")
    assert "| S17 | Nihai kabul raporu | PASS |" in report
    events = (catalog.data_dir(data_root) / "_ingest" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert {json.loads(e)["event"] for e in events} >= {"discovery", "register_start", "register_end", "item_done"}


def test_catalog_index_makes_layers_findings_assets_queryable(incoming: Path, data_root: Path) -> None:
    build_package(incoming / "TT01_v1")
    run_ingest(incoming, data_root, settle_seconds=0)

    layers = {l["layer_dataset_id"]: l for l in query_layers(data_root, DATASET)}
    assert set(layers) == {f"{DATASET}/DISTRICTS", f"{DATASET}/NEIGHBORHOODS", f"{DATASET}/AUX_BOUNDARY"}
    districts = query_layers(data_root, f"{DATASET}/DISTRICTS")
    assert len(districts) == 1 and districts[0]["feature_count"] == 2 and districts[0]["extent_minx"] is not None
    assets = {a["asset_id"]: a for a in query_assets(data_root, DATASET, "1.0.0-rc1")}
    gdb = assets[districts[0]["source_asset_id"]]
    assert gdb["kind"] == "container" and gdb["path"].endswith("Admin.gdb")
    assert any(a["parent_asset_id"] == gdb["asset_id"] for a in assets.values())

    summary = finding_summary(data_root, DATASET, "1.0.0-rc1")
    assert summary["records"] == summary["unique_exception_ids"] == 3
    assert summary["by_origin"] == {"package_exception_register": 2, "ingest_derived": 1}
    overlap = query_findings(data_root, DATASET, "1.0.0-rc1", category="DISTRICT_OVERLAP")
    assert overlap[0]["layer_dataset_id"] == f"{DATASET}/DISTRICTS" and overlap[0]["source_file"].endswith("EXCEPTION_REGISTER.csv")

    predicates = {r["predicate"] for r in query_relations(data_root, DATASET, "1.0.0-rc1")}
    assert {"registered_from_package", "layer_of", "stored_in", "part_of_version", "documents", "evidenced_by"} <= predicates
    completeness = {c["item"]: c["state"] for c in query_completeness(data_root, DATASET, "1.0.0-rc1")}
    assert completeness["temporal_coverage"] == "unknown" and completeness["semantic_definitions"] == "missing"
    assert completeness["crs_wkt"] == "present" and completeness["license_document"] == "missing"
    assert query_record_issues(data_root, DATASET, "1.0.0-rc1") == []


# ----------------------------------------------------------------------------- tekrar ve çakışma

def test_rerun_is_idempotent(incoming: Path, data_root: Path) -> None:
    build_package(incoming / "TT01_v1")
    run_ingest(incoming, data_root, settle_seconds=0)
    final = catalog.version_dir(data_root, DATASET, "1.0.0-rc1")
    before = _tree(final)
    second = run_ingest(incoming, data_root, settle_seconds=0)
    assert [c.state for c in second.candidates] == ["ALREADY_REGISTERED"]
    assert second.results == [] and second.ok
    assert _tree(final) == before


def test_same_version_different_content_is_blocked(incoming: Path, data_root: Path) -> None:
    package = build_package(incoming / "TT01_v1")
    run_ingest(incoming, data_root, settle_seconds=0)
    final = catalog.version_dir(data_root, DATASET, "1.0.0-rc1")
    before = _tree(final)
    _rewrite(package, "PACKAGE/README.md", "# farklı içerik\n")
    summary = run_ingest(incoming, data_root, settle_seconds=0)
    (r,) = summary.results
    assert r.technical == "BLOCKED" and "VERSION_CONFLICT" in r.stop_reason and r.user_decision
    assert _tree(final) == before


def test_new_version_keeps_prior_version_intact(incoming: Path, data_root: Path, tmp_path: Path) -> None:
    build_package(incoming / "TT01_v1")
    run_ingest(incoming, data_root, settle_seconds=0)
    v1 = catalog.version_dir(data_root, DATASET, "1.0.0-rc1")
    before = _tree(v1)
    build_package(incoming / "TT01_v2", version="1.0.0")
    summary = run_ingest(incoming, data_root, settle_seconds=0)
    (r,) = summary.results
    assert r.technical == "PASS" and r.prior_versions_unchanged is True
    assert _tree(v1) == before
    versions = query_versions(data_root, DATASET)
    assert sorted(v["version"] for v in versions) == ["1.0.0", "1.0.0-rc1"]
    # Sıra kayıt zamanına göredir; iki kayıt aynı saniyeye düşebilir, o zaman sürüm adı belirler.
    assert versions == sorted(versions, key=lambda v: (v["registered_at"], v["version"]))
    assert query_versions(data_root, DATASET) == versions
    assert json.loads((v1.parent / "dataset.json").read_text(encoding="utf-8"))["versions"] == ["1.0.0", "1.0.0-rc1"]


# ----------------------------------------------------------------------------- hazır olmayan / kötü niyetli paketler

def test_unready_inputs_are_not_processed(incoming: Path, data_root: Path) -> None:
    build_package(incoming / "settling")
    marked = build_package(incoming / "marked")
    (marked / "INCOMPLETE_DO_NOT_TRANSFER.txt").write_text("x", encoding="utf-8")
    partial = build_package(incoming / "partial")
    (partial / "01_ADMIN" / "big.tif.part").write_bytes(b"x")
    (incoming / "not_a_package").mkdir()
    (incoming / "HD99.zip").write_bytes(b"PK\x03\x04")

    states = {c.path.name: c.state for c in discover(incoming, data_root, settle_seconds=3600)}
    assert states == {"settling": "SETTLING", "marked": "INCOMPLETE", "partial": "INCOMPLETE",
                      "not_a_package": "NOT_A_PACKAGE", "HD99.zip": "ARCHIVE"}
    summary = run_ingest(incoming, data_root, settle_seconds=3600)
    assert summary.results == []
    assert catalog.list_versions(data_root) == []


def test_copy_in_progress_is_detected_from_manifest(incoming: Path, data_root: Path) -> None:
    package = build_package(incoming / "copying")
    (package / "PACKAGE" / "README.md").unlink()
    (c,) = discover(incoming, data_root, settle_seconds=0)
    assert c.state == "INCOMPLETE" and "eksik 1" in c.reason


def test_links_inside_package_are_rejected(incoming: Path, data_root: Path, tmp_path: Path) -> None:
    package = build_package(incoming / "linked")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    link = package / "01_ADMIN" / "linked_dir"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        try:  # Windows: sembolik bağlantı yönetici ister, junction istemez (asıl risk de budur)
            import _winapi

            _winapi.CreateJunction(str(outside), str(link))
        except (ImportError, OSError, AttributeError):
            pytest.skip("bu ortamda bağlantı/junction oluşturulamıyor")
    (c,) = discover(incoming, data_root, settle_seconds=0)
    assert c.state == "UNSAFE"
    (r,) = run_ingest(incoming, data_root, settle_seconds=0).results
    assert r.technical == "BLOCKED" and catalog.list_versions(data_root) == []


def test_path_traversal_in_manifest_is_invalid(incoming: Path, data_root: Path) -> None:
    package = build_package(incoming / "evil")
    manifest = json.loads((package / "PACKAGE" / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["sha256_files"].append({"path": "../../escape.txt", "bytes": 1, "sha256": "0" * 64})
    (package / "PACKAGE" / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    (c,) = discover(incoming, data_root, settle_seconds=0)
    assert c.state == "INVALID" and "manifest_unsafe_path" in c.reason
    (r,) = run_ingest(incoming, data_root, settle_seconds=0).results
    assert r.technical == "FAIL" and r.stages["S02"].status == "FAIL"


def test_unsupported_format_is_blocked_with_support_gap_report(incoming: Path, data_root: Path) -> None:
    package = build_package(incoming / "raster")
    (package / "02_LST").mkdir()
    (package / "02_LST" / "lst.tif").write_bytes(b"II*\x00fake")
    (package / "02_LST" / "mystery.bin").write_bytes(b"\x00")
    write_manifest(package)
    before = _tree(package)
    (r,) = run_ingest(incoming, data_root, settle_seconds=0).results
    assert r.technical == "BLOCKED" and r.stop_reason.startswith("format_not_supported") and r.user_decision
    assert r.stages["S04"].status == "BLOCKED" and r.stages["S13"].status == "NOT_RUN"
    assert r.format_plan["unsupported_files"] == ["02_LST/lst.tif", "02_LST/mystery.bin"]
    gap = json.loads((r.folder / "SUPPORT_GAP.json").read_text(encoding="utf-8"))
    assert set(gap["unsupported_by_suffix"]) == {".tif", ".bin"} and gap["unsupported_by_suffix"][".tif"]["files"] == 1
    assert gap["recognised_sources"] == {"filegdb_vector": ["01_ADMIN/SOURCE/Admin.gdb"]}
    assert gap["dataset_id"] == DATASET and gap["next_steps"]
    assert catalog.list_versions(data_root) == [] and _tree(package) == before


def test_declared_but_unsupported_data_kind_is_blocked(incoming: Path, data_root: Path) -> None:
    build_package(incoming / "met", release_overrides={"data_kinds": ["met_timeseries"]})
    (r,) = run_ingest(incoming, data_root, settle_seconds=0).results
    assert r.technical == "BLOCKED" and "met_timeseries" in r.stop_reason
    assert catalog.list_versions(data_root) == []


@pytest.mark.parametrize("overrides, reason", [
    ({"transfer_authorization": None}, "authorization_statement_missing"),
    ({"not_released_as_public_open_data": None}, "access_classification_unknown"),
])
def test_authorization_decisions_stop_automation(incoming: Path, data_root: Path, overrides, reason) -> None:
    build_package(incoming / "auth", release_overrides=overrides)
    (r,) = run_ingest(incoming, data_root, settle_seconds=0).results
    assert r.technical == "BLOCKED" and reason in r.stop_reason and r.user_decision
    assert r.stages["S09"].status == "BLOCKED"
    assert catalog.list_versions(data_root) == []


def test_quality_status_is_never_upgraded(incoming: Path, data_root: Path) -> None:
    build_package(incoming / "vague", release_overrides={"status": "READY_FOR_CONTROLLED_TRANSFER"})
    (r,) = run_ingest(incoming, data_root, settle_seconds=0).results
    assert r.technical == "PASS" and r.quality == "QA_PENDING"


def test_duplicate_exception_ids_fail_registration(incoming: Path, data_root: Path) -> None:
    dup = ["TT01-DIST-OV-01", "DISTRICT_OVERLAP", "Districts", "ADALAR", 1, "KADIKÖY", 2, "1.2", "", "x", "Do not alter", "OPEN_OWNER_REVIEW"]
    build_package(incoming / "dup", extra_register_rows=[dup])
    (r,) = run_ingest(incoming, data_root, settle_seconds=0).results
    assert r.technical == "FAIL" and r.stages["S11"].status == "FAIL"
    assert catalog.list_versions(data_root) == []


# ----------------------------------------------------------------------------- kurtarma ve kilit

def test_recovery_quarantines_half_finished_work(incoming: Path, data_root: Path, tmp_path: Path) -> None:
    staging = catalog.data_dir(data_root) / "_staging" / "ing-crashed"
    staging.mkdir(parents=True)
    (staging / "payload.bin").write_bytes(b"x")
    # Yayından sonra, son doğrulamadan önce çöken bir kayıt:
    pkg = build_package(tmp_path / "half")
    assert register_package(pkg, data_root).status == "PASS"
    final = catalog.version_dir(data_root, DATASET, "1.0.0-rc1")
    os.chmod(final / "version.json", stat.S_IREAD | stat.S_IWRITE)
    record = json.loads((final / "version.json").read_text(encoding="utf-8"))
    record["registration"]["status"] = "PENDING_FINAL_VERIFICATION"
    (final / "version.json").write_text(json.dumps(record), encoding="utf-8")

    summary = run_ingest(incoming, data_root, settle_seconds=0)
    reasons = sorted(a["reason"] for a in summary.recovery)
    assert reasons == ["abandoned_staging", "catalog_bookkeeping", "incomplete_publication:PENDING_FINAL_VERIFICATION"]
    assert not final.exists() and catalog.list_versions(data_root) == []
    failed = list((catalog.data_dir(data_root) / "_failed").iterdir())
    assert len(failed) == 2 and all((f / "RECOVERY.json").is_file() for f in failed)
    assert json.loads((final.parent / "dataset.json").read_text(encoding="utf-8"))["versions"] == []
    # Kaynak paket dokunulmadan kaldı ve güvenle yeniden denenebilir:
    states = {c.path.name: c.state for c in discover(tmp_path, data_root, settle_seconds=0)}
    assert states["half"] == "READY"


def test_concurrent_ingest_is_refused(incoming: Path, data_root: Path) -> None:
    with IngestLock(data_root):
        with pytest.raises(IngestLocked):
            run_ingest(incoming, data_root, settle_seconds=0)
    assert run_ingest(incoming, data_root, settle_seconds=0).results == []
    with IngestLock(data_root):
        pass
    (catalog.data_dir(data_root) / "_ingest" / "ingest.lock").write_text("{}", encoding="utf-8")  # çökme sonrası kalan kilit
    with pytest.raises(IngestLocked):
        run_ingest(incoming, data_root, settle_seconds=0)
    assert release_stale_lock(data_root) is True
    run_ingest(incoming, data_root, settle_seconds=0)


# ----------------------------------------------------------------------------- bağımsız denetim uzantı noktası

def test_audit_extension_point_records_result_without_changing_technical_status(incoming: Path, data_root: Path) -> None:
    """Bu sürümde denetçi yok; ileride eklenecek denetçinin sonucu S16'ya ve denetim eksenine yazılır."""
    calls = []

    def auditor(root: Path, dataset_id: str, version: str, source: Path) -> tuple[str, str]:
        calls.append((dataset_id, version, source.name))
        return "FAILED", "örnek bulgu"

    build_package(incoming / "TT01_v1")
    (r,) = run_ingest(incoming, data_root, settle_seconds=0, auditor=auditor).results
    assert calls == [(DATASET, "1.0.0-rc1", "TT01_v1")]
    assert (r.technical, r.audit, r.stages["S16"].status) == ("PASS", "FAILED", "FAIL")


# ----------------------------------------------------------------------------- eski kayıt (HD01 biçimi)

def test_legacy_record_is_indexed_without_modification(package: Path, data_root: Path) -> None:
    """HD01 biçimindeki eski kayıt: AUX durumu istisna listesinde, aynı kimlik iki kez, köken/parmak izi yok."""
    assert register_package(package, data_root).status == "PASS"
    final = catalog.version_dir(data_root, DATASET, "1.0.0-rc1")
    for name in ("version.json", "quality.json"):
        os.chmod(final / name, stat.S_IREAD | stat.S_IWRITE)
    quality = json.loads((final / "quality.json").read_text(encoding="utf-8"))
    for e in quality["exceptions"]:
        e.pop("origin")
    quality["exceptions"].append({**quality["exceptions"][0], "exception_id": "TT01-AUX-01", "category": "AUXILIARY_SELF_INTERSECTION",
                                  "dataset": "Aux_Boundary"})
    quality["exceptions"].append({"exception_id": "TT01-AUX-01", "category": "AUXILIARY_LAYER_STATUS", "dataset": "Aux_Boundary",
                                  "evidence": "geometri durumu: SELF_INTERSECTION_EXCLUDE_FROM_CORE; tür: x", "source": "PACKAGE/QA/TT01_GATES.json"})
    for key in ("layer_status", "summary_by_origin"):
        quality.pop(key)
    (final / "quality.json").write_text(json.dumps(quality, ensure_ascii=False), encoding="utf-8")
    record = json.loads((final / "version.json").read_text(encoding="utf-8"))
    record["source"].pop("package_fingerprint")
    (final / "version.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    before = _tree(final)

    rebuild_index(data_root)
    assert _tree(final) == before, "dizin kurulumu kayıtları değiştirmemeli"
    codes = {i["code"] for i in query_record_issues(data_root, DATASET, "1.0.0-rc1")}
    assert codes == {"DUPLICATE_EXCEPTION_ID", "LAYER_STATUS_COUNTED_AS_EXCEPTION", "FINDING_ORIGIN_NOT_RECORDED",
                     "PACKAGE_FINGERPRINT_NOT_RECORDED", "RECORD_FILES_WRITABLE"}
    s = finding_summary(data_root, DATASET, "1.0.0-rc1")
    assert (s["records"], s["unique_exception_ids"], s["duplicated_exception_ids"]) == (5, 4, ["TT01-AUX-01"])
    assert s["by_kind"] == {"finding": 3, "documented_condition": 1, "layer_status": 1}
    assert s["findings_from_package_register"] == 3  # köken kaynak dosyasından çıkarıldı (eklenen AUX satırının kaynağı da defter)
    # Eski kayıt için parmak izi payload'daki manifestten hesaplanır → aynı paket ALREADY_REGISTERED olur.
    incoming = package.parent
    states = {c.path.name: c.state for c in discover(incoming, data_root, settle_seconds=0)}
    assert states[package.name] == "ALREADY_REGISTERED"


# ----------------------------------------------------------------------------- adaptörler, API, CLI

def test_adapter_classification_and_support_matrix() -> None:
    plan = classify_paths([
        "PACKAGE/README.md", "PACKAGE/QA/register.csv", "A/SOURCE/X.metadata/info.json",
        "A/SOURCE/X.gdb/a00000001.gdbtable", "A/SOURCE/X.gdb/gdb", "B/lst_2024.tif", "C/era5.nc",
    ])
    assert plan.sources == {"filegdb_vector": ["A/SOURCE/X.gdb"]} and len(plan.documents) == 3
    assert plan.unsupported == ["B/lst_2024.tif", "C/era5.nc"] and not plan.supported
    only_gdb = classify_paths(["PACKAGE/README.md", "A/X.gdb/gdb"])
    assert only_gdb.supported and only_gdb.unsupported == []
    assert not classify_paths(["PACKAGE/README.md"]).supported  # veri kaynağı yok
    assert [a["key"] for a in support_matrix()] == ["filegdb_vector"]  # yalnız gerçekten uygulanmış olanlar


def test_api_exposes_layers_findings_formats(incoming: Path, tmp_path: Path, monkeypatch) -> None:
    from thermalis.app import create_app

    data_root = tmp_path / "root"
    monkeypatch.setenv("THERMALIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("thermalis.settings.default_data_root", lambda: data_root)
    build_package(incoming / "TT01_v1")
    run_ingest(incoming, data_root, settle_seconds=0)
    client = TestClient(create_app())
    layers = client.get("/api/data/layers", params={"dataset_id": DATASET}).json()
    assert {l["layer_name"] for l in layers} == {"Districts", "Neighborhoods", "Aux_Boundary"}
    findings = client.get("/api/data/findings", params={"dataset_id": DATASET, "version": "1.0.0-rc1"}).json()
    assert findings["summary"]["records"] == 3 and len(findings["findings"]) == 3
    assert client.get("/api/data/formats").json()[0]["key"] == "filegdb_vector"


def test_cli_ingest_and_formats(incoming: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    from thermalis.cli import main

    data_root = tmp_path / "root"
    monkeypatch.setenv("THERMALIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("thermalis.settings.default_data_root", lambda: data_root)
    assert main(["data", "formats"]) == 0
    assert main(["data", "ingest"]) == 1  # gelen klasör ayarlı değil
    assert main(["data", "set-incoming", str(incoming)]) == 0
    build_package(incoming / "TT01_v1")
    assert main(["data", "ingest", "--settle-seconds", "0", "--dry-run"]) == 0
    assert catalog.list_versions(data_root) == []
    assert main(["data", "ingest", "--settle-seconds", "0"]) == 0
    out = capsys.readouterr().out
    assert "teknik=PASS kalite=QA_CONDITIONAL denetim=NOT_CONFIGURED erişim=RESTRICTED" in out
    assert main(["data", "findings", DATASET, "1.0.0-rc1"]) == 0
    assert main(["data", "layers", DATASET]) == 0 and main(["data", "reindex"]) == 0
