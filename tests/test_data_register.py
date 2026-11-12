from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import build_package, write_manifest
from thermalis.data import catalog
from thermalis.data.integrity import sha256_file
from thermalis.data.register import register_package, verify_registered

DATASET = "THERMALIS_DATA/TT01_ADMIN_BOUNDARIES/ISTANBUL"


def _snapshot(root: Path) -> dict[str, tuple[str, float]]:
    return {p.relative_to(root).as_posix(): (sha256_file(p), p.stat().st_mtime) for p in root.rglob("*") if p.is_file()}


def _statuses(outcome) -> dict[str, str]:
    return {f"{c.id} {c.title}": c.status for c in outcome.checks}


def test_register_success(package: Path, data_root: Path) -> None:
    before = _snapshot(package)
    outcome = register_package(package, data_root, expect_status="QA_CONDITIONAL")
    assert outcome.status == "PASS", outcome.error
    assert _snapshot(package) == before, "kaynak paket değişmemeli"

    final = catalog.version_dir(data_root, DATASET, "1.0.0-rc1")
    assert outcome.location == final
    record = json.loads((final / "version.json").read_text(encoding="utf-8"))
    assert record["registration"]["status"] == "REGISTERED"
    layers = record["layers"]
    assert layers["Districts"]["feature_count"] == 2 and layers["Districts"]["role"] == "core"
    assert layers["Neighborhoods"]["feature_count"] == 3 and layers["Neighborhoods"]["published"] is True
    assert layers["Aux_Boundary"]["role"] == "auxiliary" and layers["Aux_Boundary"]["published"] is False
    assert layers["Districts"]["crs_matches_package_wkt"] is True
    assert record["crs"]["transformed"] is False
    kimlik = next(f for f in layers["Neighborhoods"]["fields"] if f["name"] == "KIMLIKNO")
    assert kimlik["null_count"] == 1

    quality = json.loads((final / "quality.json").read_text(encoding="utf-8"))
    assert quality["status"] == "QA_CONDITIONAL"
    assert quality["summary"] == {"DISTRICT_OVERLAP": 1, "NEIGHBORHOOD_OUTSIDE_PARENT": 1,
                                  "INTENTIONAL_NULL_IDENTIFIER": 1, "AUXILIARY_LAYER_STATUS": 1}

    integrity = json.loads((final / "integrity.json").read_text(encoding="utf-8"))
    assert {k: v["status"] for k, v in integrity["verifications"].items()} == {
        "source_package": "PASS", "staging_copy": "PASS", "final_location": "PASS"}

    payload_file = next((final / "payload").rglob("*.gdbtable"))
    assert not (payload_file.stat().st_mode & stat.S_IWUSR), "kopya salt okunur olmalı"

    report = (final / "TT01_DATA_INGESTION_REPORT.md").read_text(encoding="utf-8")
    assert "**PASS**" in report and "QA_CONDITIONAL" in report and "FİKİRTEPE" in report
    assert not (catalog.data_dir(data_root) / "_staging").exists() or not any((catalog.data_dir(data_root) / "_staging").iterdir())

    listed = catalog.list_versions(data_root)
    assert [(v["dataset_id"], v["version"], v["quality_status"]) for v in listed] == [(DATASET, "1.0.0-rc1", "QA_CONDITIONAL")]
    assert all(c.status == "PASS" for c in verify_registered(data_root, DATASET, "1.0.0-rc1"))


def test_existing_version_is_never_overwritten(package: Path, data_root: Path) -> None:
    assert register_package(package, data_root).status == "PASS"
    final = catalog.version_dir(data_root, DATASET, "1.0.0-rc1")
    before = (final / "version.json").read_bytes()
    second = register_package(package, data_root)
    assert second.status == "FAIL"
    assert _statuses(second)["CAT-01 Bu sürüm katalogda yok (üzerine yazma engeli)"] == "FAIL"
    assert (final / "version.json").read_bytes() == before
    assert second.report_path.is_file() and "_failed" in str(second.report_path)


def test_tampered_source_is_rejected(package: Path, data_root: Path) -> None:
    (package / "PACKAGE" / "README.md").write_text("# değiştirildi\n", encoding="utf-8")
    outcome = register_package(package, data_root)
    assert outcome.status == "FAIL"
    assert any(c.id == "INT-01" and c.status == "FAIL" for c in outcome.checks)
    assert catalog.list_versions(data_root) == []
    assert not catalog.version_dir(data_root, DATASET, "1.0.0-rc1").exists()


def test_unexpected_file_is_rejected(package: Path, data_root: Path) -> None:
    (package / "PACKAGE" / "extra.txt").write_text("x", encoding="utf-8")
    outcome = register_package(package, data_root)
    assert outcome.status == "FAIL"
    assert any(c.id == "INT-01" and "UNEXPECTED_FILE" in c.detail for c in outcome.checks)


def test_count_mismatch_leaves_no_partial_record(tmp_path: Path, data_root: Path) -> None:
    pkg = build_package(tmp_path / "pkg", counts={"Districts": 2, "Neighborhoods": 4, "Aux_Boundary": 1})
    outcome = register_package(pkg, data_root)
    assert outcome.status == "FAIL"
    assert any(c.id == "VEC-04" and "Neighborhoods" in c.title and c.status == "FAIL" for c in outcome.checks)
    assert catalog.list_versions(data_root) == []
    staging = catalog.data_dir(data_root) / "_staging"
    assert not staging.exists() or not any(staging.iterdir())
    failed = list((catalog.data_dir(data_root) / "_failed").iterdir())
    assert len(failed) == 1 and (failed[0] / "TT01_DATA_INGESTION_REPORT.md").is_file()


def test_null_identity_mismatch_fails(tmp_path: Path, data_root: Path) -> None:
    pkg = build_package(tmp_path / "pkg", null_oids=[2])
    outcome = register_package(pkg, data_root)
    assert outcome.status == "FAIL"
    assert any(c.id == "NUL-01" and c.status == "FAIL" for c in outcome.checks)


def test_expected_status_mismatch(package: Path, data_root: Path) -> None:
    outcome = register_package(package, data_root, expect_status="QA_PASS")
    assert outcome.status == "FAIL"
    assert any(c.id == "PKG-03" and c.status == "FAIL" for c in outcome.checks)


def test_verify_detects_tampering_after_registration(package: Path, data_root: Path) -> None:
    assert register_package(package, data_root).status == "PASS"
    final = catalog.version_dir(data_root, DATASET, "1.0.0-rc1")
    target = final / "payload" / "PACKAGE" / "README.md"
    os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    target.write_text("# kurcalandı\n", encoding="utf-8")
    checks = verify_registered(data_root, DATASET, "1.0.0-rc1")
    assert any(c.id == "INT-03" and c.status == "FAIL" for c in checks)


def test_api_lists_registered_data(package: Path, tmp_path: Path, monkeypatch) -> None:
    from thermalis.app import create_app

    data_root = tmp_path / "root"
    monkeypatch.setenv("THERMALIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("thermalis.settings.default_data_root", lambda: data_root)
    assert register_package(package, data_root).status == "PASS"
    client = TestClient(create_app())
    versions = client.get("/api/data/versions").json()
    assert versions[0]["dataset_id"] == DATASET and versions[0]["exception_count"] == 4
    detail = client.get("/api/data/version", params={"dataset_id": DATASET, "version": "1.0.0-rc1"}).json()
    assert detail["quality"]["status"] == "QA_CONDITIONAL"
    assert "files" not in detail["integrity"]
    assert client.get("/api/data/version", params={"dataset_id": "../../x", "version": "1"}).status_code == 400


def test_manifest_rejects_unsafe_paths(package: Path, data_root: Path) -> None:
    manifest = json.loads((package / "PACKAGE" / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["sha256_files"].append({"path": "../escape.txt", "bytes": 1, "sha256": "0" * 64})
    (package / "PACKAGE" / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    outcome = register_package(package, data_root)
    assert outcome.status == "FAIL"
    assert any(c.id == "PKG-01" and "manifest_unsafe_path" in c.detail for c in outcome.checks)





def test_crs_mismatch_fails(package: Path, data_root: Path) -> None:
    crs_file = package / "PACKAGE" / "CRS_AND_UNITS.json"
    crs = json.loads(crs_file.read_text(encoding="utf-8"))
    crs["native_wkt"] = crs["native_wkt"].replace('"Central_Meridian",30.0', '"Central_Meridian",27.0')
    crs_file.write_text(json.dumps(crs), encoding="utf-8")
    write_manifest(package)
    outcome = register_package(package, data_root)
    assert outcome.status == "FAIL"
    assert any(c.id == "VEC-06" and c.status == "FAIL" for c in outcome.checks)


def test_domain_mismatch_fails(package: Path, data_root: Path) -> None:
    domains_file = package / "PACKAGE" / "CODED_DOMAINS.json"
    domains = json.loads(domains_file.read_text(encoding="utf-8"))
    domains[0]["coded_values"]["1421"] = "KADIKOY"
    domains_file.write_text(json.dumps(domains, ensure_ascii=False), encoding="utf-8")
    write_manifest(package)
    outcome = register_package(package, data_root)
    assert outcome.status == "FAIL"
    assert any(c.id == "DOM-01" and c.status == "FAIL" for c in outcome.checks)
