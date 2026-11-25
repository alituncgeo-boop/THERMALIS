"""Faz 05 ön koşulları: genel sürüm taşıma, ek kayıtlar, karşılaştırma ve doğrulanmış malzemeleştirme (sahte R2)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from s3fake import make_s3_backend
from thermalis.cloud import layout
from thermalis.cloud.access import CacheCapacityExceeded, InsufficientDiskSpace, materialize_asset
from thermalis.cloud.catalog_sync import sync_catalog
from thermalis.cloud.compare import compare_release
from thermalis.cloud.e2e import prepare_legacy_release, run_migration_rehearsal
from thermalis.cloud.migrate import MigrationRefused, migrate_release, plan_migration, verify_migration
from thermalis.data import catalog, contract
from thermalis.objectstore.cache import ObjectCache
from thermalis.objectstore.errors import TransientError
from thermalis.objectstore.publication import committed_releases, release_prefix

BUCKET = "thermalis-test-bucket"
DS = "THERMALIS_DATA/SYN98_MIGRATION/TESTKENT"
LID = f"{DS}/NEIGHBORHOODS"


@pytest.fixture()
def legacy(tmp_path: Path):
    data_root = tmp_path / "THERMALIS_DATA"
    data_root.mkdir()
    prepare_legacy_release(tmp_path, data_root, dataset_id=DS, package_id="SYN98_MIGRATION_v1.0.0", restricted=False)
    return data_root


def _plan(data_root: Path, **kw):
    return plan_migration(data_root, DS, "1.0.0", provider="r2", bucket=BUCKET, **kw)


def test_full_rehearsal_passes_against_fake_r2(tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    steps = run_migration_rehearsal(store, provider="r2", bucket=BUCKET, run_id="unit", cache_root=tmp_path / "cache")
    failed = [(s.name, s.detail) for s in steps if s.status == "FAIL"]
    assert not failed, failed
    assert [s.name for s in steps if s.status == "PASS"][-1] == "local_release_untouched"


def test_plan_is_deterministic_and_preserves_history(legacy: Path) -> None:
    a, b = _plan(legacy), _plan(legacy)
    assert a.records == b.records and a.operation_id == b.operation_id  # sürdürme için birebir aynı baytlar
    folder = catalog.version_dir(legacy, DS, "1.0.0")
    for name in ("version.json", "quality.json", "integrity.json"):
        assert a.records[name] == (folder / name).read_bytes()  # bayt bayt; yeniden üretim yok
    facts = json.loads(a.records["layer_facts.json"])
    assert facts["source"] == "computed_from_local_payload" and set(facts["layers"]) == {"Districts", "Neighborhoods", "Aux_Boundary"}
    ids = json.loads(a.records["quality_identifiers.json"])["layers"][LID]
    assert all(v["GLOBALID"].startswith("{") for v in ids.values())
    migration = json.loads(a.records["migration.json"])
    assert migration["access_class_changed"] is False and migration["local_verification"]["status"] == "PASS"
    assert "PACKAGE_FINGERPRINT_NOT_RECORDED" in {c for c, _ in migration["record_issues_at_migration"]}
    sem = [json.loads(l) for l in a.records["semantic_verifications.jsonl"].decode().splitlines()]
    assert sem and all(r["entry_hash"] and r["dataset_id"].startswith(DS + "/") for r in sem)
    assert any(n.startswith("audits/aud-") for n in a.records)
    rever = [json.loads(l) for l in a.records["reverification_history.jsonl"].decode().splitlines()]
    assert rever and all(r["dataset_id"] == DS and r["version"] == "1.0.0" for r in rever)
    assert a.warnings  # parmak izi kayıtta yok: açıkça uyarılır


def test_local_integrity_failure_refuses_migration(legacy: Path) -> None:
    folder = catalog.version_dir(legacy, DS, "1.0.0")
    victim = next((folder / "payload").rglob("*.gdbtable"))
    os.chmod(victim, stat.S_IREAD | stat.S_IWRITE)
    victim.write_bytes(victim.read_bytes() + b"x")
    with pytest.raises(MigrationRefused) as exc:
        _plan(legacy)
    assert exc.value.code == "local_integrity_failed"


def test_restricted_without_authorization_and_unknown_version(legacy: Path, tmp_path: Path) -> None:
    with pytest.raises(MigrationRefused) as exc:
        plan_migration(legacy, DS, "9.9.9", provider="r2", bucket=BUCKET)
    assert exc.value.code == "local_version_not_found"
    prepare_legacy_release(tmp_path / "r", legacy, dataset_id="THERMALIS_DATA/SYN97_MIGRATION/TESTKENT",
                           package_id="SYN97_MIGRATION_v1.0.0", restricted=True)
    with pytest.raises(MigrationRefused) as exc:
        plan_migration(legacy, "THERMALIS_DATA/SYN97_MIGRATION/TESTKENT", "1.0.0", provider="r2", bucket=BUCKET)
    assert exc.value.code == "authorization_required"


def test_interrupted_migration_resumes_with_same_operation(legacy: Path, tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    plan = _plan(legacy)
    original = server.put_object
    armed = {"done": False}

    def crash(**kw):
        if "/records/" in kw["Key"] and not armed["done"]:
            armed["done"] = True
            server.fail_next("put_object", "before", times=4)
        return original(**kw)

    server.put_object = crash
    with pytest.raises(TransientError):
        migrate_release(store, plan)
    server.put_object = original
    assert list(committed_releases(store)) == [] and not store.exists(layout.catalog_entry_key(DS, "1.0.0"))
    result = migrate_release(store, _plan(legacy))  # yeni süreç: aynı belirlenimci plan
    assert result["status"] == "MIGRATED" and verify_migration(store, plan)["status"] == "PASS"
    assert migrate_release(store, _plan(legacy))["status"] == "ALREADY_MIGRATED"


def test_conflicting_cloud_version_is_never_overwritten(legacy: Path, tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    store.put_bytes(layout.catalog_entry_key(DS, "1.0.0"), json.dumps({"manifest_sha256": "0" * 64, "operation_id": "other"}).encode(),
                    create_only=True, operation_id="other")
    with pytest.raises(MigrationRefused) as exc:
        migrate_release(store, _plan(legacy))
    assert exc.value.code == "version_already_in_cloud"
    assert list(committed_releases(store)) == []


def test_comparison_and_dual_period_catalog(legacy: Path, tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    migrate_release(store, _plan(legacy))
    report = compare_release(legacy, store, DS, "1.0.0", provider="r2", bucket=BUCKET)
    assert report["status"] == "PASS", report["differences"]
    assert {"special_records", "content_id", "semantics", "findings", "domains"} <= set(report["compared_fields"])
    sync_catalog(store, legacy, provider="r2", bucket=BUCKET)
    from thermalis.data.catalog_index import _query

    assert [r["backend"] for r in _query(legacy, "SELECT backend FROM storage_locations WHERE dataset_id=?", (DS,))] == ["local"]
    assert _query(legacy, "SELECT same_fingerprint FROM cloud_copies")[0]["same_fingerprint"] == 1
    assert contract.access(legacy, LID, authorized=True)["absolute_path"]  # geçiş yok: yerel yol aynen
    assert compare_release(legacy, store, DS, "2.0.0", provider="r2", bucket=BUCKET)["status"] == "FAIL"


def _cloud_only(store, tmp_path: Path) -> Path:
    root = tmp_path / "CLOUD_ONLY"
    root.mkdir()
    sync_catalog(store, root, provider="r2", bucket=BUCKET, full=True)
    return root


def test_cloud_only_catalog_keeps_content_ids_and_globalids(legacy: Path, tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    migrate_release(store, _plan(legacy))
    root = _cloud_only(store, tmp_path)
    d_local, d_cloud = contract.descriptor(legacy, LID), contract.descriptor(root, LID)
    assert d_cloud["content"]["content_id"] == d_local["content"]["content_id"] and d_cloud["storage"]["backend"] == "r2"
    assert d_cloud["content"]["source"] == "records/layer_facts.json"
    special = contract.quality(root, LID)["special_records"]
    assert special == contract.quality(legacy, LID)["special_records"] and all(s["globalid"] for s in special)
    # Kullanıcı doğrulamalarının yetkili kaydı yerel zincirli günlüktür (bu kökte yok): burada uygulanmaz, ama
    # yayınla birlikte arşiv kopyası olarak saklanır; yerel günlükle (compare testi) aynen geçerli kalır.
    assert contract.field_info(root, LID, None, "YAKA")["status"] != "USER_VERIFIED"
    assert (root / "data" / "_cloud" / "releases" / "SYN98_MIGRATION" / "TESTKENT" / "1.0.0" / "semantic_verifications.jsonl").is_file()


def test_materialization_limits_and_local_passthrough(legacy: Path, tmp_path: Path) -> None:
    store, server = make_s3_backend(tmp_path)
    migrate_release(store, _plan(legacy))
    root = _cloud_only(store, tmp_path)
    local = materialize_asset(legacy, None, None, LID)
    assert local["backend"] == "local" and local["materialized"] is False and Path(local["path"]).is_dir()
    small = ObjectCache(tmp_path / "c1", 1024 * 1024)
    small.max_bytes = 1000  # varlıktan küçük çalışma alanı (20 GB sınırı bir veri seti boyut sınırı değildir)
    with pytest.raises(CacheCapacityExceeded):
        materialize_asset(root, store, small, LID)
    with pytest.raises(InsufficientDiskSpace):
        materialize_asset(root, store, ObjectCache(tmp_path / "c2", 1024 ** 3), LID, disk_free=lambda p: 1024)
    m = materialize_asset(root, store, ObjectCache(tmp_path / "c3", 1024 ** 3), LID)
    assert m["materialized"] and Path(m["path"]).is_dir() and m["verification"] == "sha256_per_object_against_commit"
    gets = server.calls["get_object"]
    materialize_asset(root, store, ObjectCache(tmp_path / "c3", 1024 ** 3), LID)
    assert server.calls["get_object"] == gets  # önbellekte: yeniden indirme yok


def test_materialize_api_enforces_physical_access(legacy: Path, tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from thermalis.app import create_app
    from thermalis.settings import CacheSettings, Settings, component_token, save_settings

    monkeypatch.setenv("THERMALIS_CONFIG_DIR", str(tmp_path / "config"))
    save_settings(Settings(data_root=str(legacy), cache=CacheSettings(cache_dir=str(tmp_path / "cache"))))
    client = TestClient(create_app(watch=False))
    r = client.post("/api/data/v1/materialize", json={"id": LID})
    assert r.status_code == 200 and r.json()["backend"] == "local"  # PUBLIC sentetik veri, yerel yol
    prepare_legacy_release(tmp_path / "r", legacy, dataset_id="THERMALIS_DATA/SYN97_MIGRATION/TESTKENT",
                           package_id="SYN97_MIGRATION_v1.0.0", restricted=True)
    restricted = "THERMALIS_DATA/SYN97_MIGRATION/TESTKENT/NEIGHBORHOODS"
    denied = client.post("/api/data/v1/materialize", json={"id": restricted})
    assert denied.status_code == 403 and denied.json()["detail"]["code"] == "physical_access_not_authorized"
    ok = client.post("/api/data/v1/materialize", json={"id": restricted}, headers={"X-Thermalis-Component-Token": component_token()})
    assert ok.status_code == 200
    assert client.get("/api/data/v1/access", params={"id": LID}).json()["materialize"] is None  # yerel: gerekmez


def test_new_cloud_packages_also_keep_globalids(tmp_path: Path, data_root: Path) -> None:
    from conftest import build_package
    from thermalis.cloud.intake import CloudIntake
    from thermalis.cloud.upload import upload_package

    store, server = make_s3_backend(tmp_path)
    ds = "THERMALIS_DATA/SYN96_GLOBALID/TESTKENT"
    root = build_package(tmp_path / "src" / "SYN96_v1.0.0", version="1.0.0", dataset_id=ds, package_id="SYN96_v1.0.0",
                         release_overrides={"not_released_as_public_open_data": None, "access_classification": "PUBLIC"}, globalid=True)
    upload_package(store, root, provider="r2", bucket=BUCKET)
    CloudIntake(store, store, data_root, provider="r2", bucket=BUCKET, intake_bucket=BUCKET, namespace="",
                staging_root=tmp_path / "st", staging_max_bytes=10 ** 10, disk_free=lambda p: 10 ** 12).scan()
    special = contract.quality(data_root, f"{ds}/NEIGHBORHOODS")["special_records"]
    assert special and all(s["globalid"] for s in special)
    commit = json.loads(store.open_read(release_prefix(ds, "1.0.0") + "_COMMIT.json").read())
    assert any(o["key"].endswith("records/quality_identifiers.json") for o in commit["objects"])
