"""Faz 04: bulut paket yükleme, bulut kabulü, yayın ve merkezi katalog (sahte R2 ile; gerçek veri yok)."""

from __future__ import annotations

import hashlib
import io
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import build_package, write_ready_marker
from s3fake import make_s3_backend
from thermalis.cloud import authorization as auth
from thermalis.cloud import layout
from thermalis.cloud.catalog_sync import cloud_record_folders, load_sync_state, mirror_root, sync_catalog, verify_cloud_version
from thermalis.cloud.intake import CloudIntake
from thermalis.cloud.upload import ProgressReporter, UploadRejected, plan_package, upload_package
from thermalis.data import catalog, contract
from thermalis.data.catalog_index import _query, rebuild_index
from thermalis.data.ingest import load_intake, run_ingest
from thermalis.objectstore.errors import TransientError
from thermalis.objectstore.publication import committed_releases, inspect_release, release_prefix

BUCKET = "thermalis-test-bucket"
DS = "THERMALIS_DATA/SYN01_SYNTHETIC/TESTKENT"
PUBLIC = {"not_released_as_public_open_data": None, "access_classification": "PUBLIC"}


def _pkg(tmp: Path, *, version: str = "1.0.0", package_id: str | None = None, restricted: bool = False, extra: str | None = None,
         ds: str = DS) -> Path:
    pid = package_id or f"SYN01_SYNTHETIC_v{version}"
    return build_package(tmp / "src" / pid, version=version, dataset_id=ds, package_id=pid,
                         release_overrides=None if restricted else PUBLIC, extra_field=extra)


def _intake(store, data_root: Path, tmp: Path, **kw) -> CloudIntake:
    options = {"staging_root": tmp / "staging", "staging_max_bytes": 10 ** 10, "disk_free": lambda p: 10 ** 12}
    options.update(kw)
    return CloudIntake(store, store, data_root, provider="r2", bucket=BUCKET, intake_bucket=BUCKET, namespace="", **options)


def _upload(store, root: Path, **kw):
    return upload_package(store, root, provider="r2", bucket=BUCKET, **kw)


def _tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture()
def cloud(tmp_path: Path):
    store, server = make_s3_backend(tmp_path)
    return store, server


# ----------------------------------------------------------------------------- uçtan uca

def test_upload_then_cloud_intake_end_to_end(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    root = _pkg(tmp_path)
    before = _tree(root)
    report = ProgressReporter(0, 0, out=io.StringIO())
    plan = plan_package(root)
    report.total_bytes, report.total_files = plan.total_bytes, len(plan.files)
    result = _upload(store, root, reporter=report, plan=plan)
    assert result["status"] == "UPLOADED" and report.files_done == len(plan.files) and report.done_bytes == plan.total_bytes
    assert _tree(root) == before  # kaynak paket değişmedi
    assert store.exists(layout.ready_key(plan.package_id)) and store.exists(layout.announcement_key(plan.package_id))

    scan = _intake(store, data_root, tmp_path).scan()
    assert scan.status == "ok" and [o.status for o in scan.outcomes] == ["ACCEPTED"], scan.outcomes
    prefix = release_prefix(DS, "1.0.0")
    assert inspect_release(store, prefix, full=True).status == "COMMITTED"
    assert store.exists(layout.catalog_entry_key(DS, "1.0.0"))
    state = json.loads(store.open_read(layout.intake_state_key(plan.package_id, plan.manifest_sha256)).read())
    assert state["status"] == "ACCEPTED" and state["release_prefix"] == prefix

    # Merkezi katalog: bulut kaydı yerelde yalnız türetilmiş yansı olarak; veri dosyası kopyası yok.
    assert not catalog.version_dir(data_root, DS, "1.0.0").exists()
    (folder,) = cloud_record_folders(data_root)
    assert not list((folder / "payload").rglob("*.gdbtable"))
    loc = _query(data_root, "SELECT * FROM storage_locations WHERE dataset_id=?", (DS,))
    assert [(r["backend"], r["version"]) for r in loc] == [("r2", "1.0.0")]
    lid = f"{DS}/NEIGHBORHOODS"
    d = contract.descriptor(data_root, lid)
    assert d["quality"]["status"] == "QA_CONDITIONAL" and d["storage"]["backend"] == "r2" and d["access"]["classification"] == "PUBLIC"
    access = contract.access(data_root, lid)
    assert access["absolute_path"] is None and access["storage"]["gdal_path"].startswith(f"/vsis3/{BUCKET}/{prefix}payload/")
    assert any(f["field"] == "ILCEKN" for f in contract.semantic_dictionary(data_root, lid)["fields"])
    entry = load_intake(data_root)["packages"][f"r2://{BUCKET}/{layout.package_prefix(plan.package_id)}"]
    assert (entry["state"], entry["source"], entry["storage"]) == ("REGISTERED", "cloud", "r2")
    assert not any((tmp_path / "staging").glob("w-*"))  # geçici alan yalnız başarıdan sonra temizlendi


def test_rescan_of_settled_package_is_quiet(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    _upload(store, _pkg(tmp_path))
    processor = _intake(store, data_root, tmp_path)
    processor.scan()
    intake_before = (catalog.data_dir(data_root) / "_ingest" / "intake.json").read_text(encoding="utf-8")
    calls = dict(server.calls)
    again = processor.scan()
    assert again.outcomes == [] and again.sync["unchanged"] == 1
    new = {k: server.calls[k] - calls.get(k, 0) for k in server.calls}
    assert new.get("put_object", 0) == 0 and new.get("get_object", 0) == 0 and new.get("head_object", 0) == 0
    assert (catalog.data_dir(data_root) / "_ingest" / "intake.json").read_text(encoding="utf-8") == intake_before


# ----------------------------------------------------------------------------- yükleme

def test_interrupted_upload_resumes_and_incomplete_package_is_invisible(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    root = _pkg(tmp_path)
    original = server.put_object
    count = {"n": 0}

    def flaky(**kw):
        count["n"] += 1
        if count["n"] == 6:
            server.fail_next("put_object", "before", times=4)
        return original(**kw)

    server.put_object = flaky
    with pytest.raises(TransientError):
        _upload(store, root)
    server.put_object = original
    plan = plan_package(root)
    assert not store.exists(layout.ready_key(plan.package_id))  # işaret yok → keşfedilmez
    assert _intake(store, data_root, tmp_path).scan().discovered == 0
    out = io.StringIO()
    result = _upload(store, root, reporter=ProgressReporter(plan.total_bytes, len(plan.files), out=out))
    assert result["status"] == "UPLOADED" and 0 < result["bytes_sent"] < plan.total_bytes  # yalnız eksik dosyalar
    assert "kalan" in out.getvalue() and "/s" in out.getvalue()
    assert _upload(store, root)["status"] == "ALREADY_UPLOADED"  # yineleme: hiçbir şey yüklenmez


def test_uploader_rejects_invalid_packages_without_network(tmp_path: Path) -> None:
    root = _pkg(tmp_path)
    (root / "extra.bin").write_bytes(b"not in manifest")
    with pytest.raises(UploadRejected) as exc:
        plan_package(root)
    assert exc.value.code == "source_integrity_failed"
    root2 = _pkg(tmp_path, version="1.0.1")
    target = next(p for p in root2.rglob("*.gdbtable"))
    target.write_bytes(target.read_bytes() + b"x")
    with pytest.raises(UploadRejected):
        plan_package(root2)
    root3 = _pkg(tmp_path, version="1.0.2")
    (root3 / "INCOMPLETE_DO_NOT_TRANSFER.txt").write_text("x", encoding="utf-8")
    with pytest.raises(UploadRejected) as exc:
        plan_package(root3)
    assert exc.value.code == "package_incomplete_marker"
    with pytest.raises(UploadRejected):
        plan_package(tmp_path / "does-not-exist")


def test_upload_conflicts_are_explicit(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    _upload(store, _pkg(tmp_path, package_id="SYN01_SAME"))
    other = _pkg(tmp_path / "b", package_id="SYN01_SAME", extra="AREA_HA")  # aynı paket kimliği, farklı içerik
    with pytest.raises(UploadRejected) as exc:
        _upload(store, other)
    assert exc.value.code == "package_id_conflict"
    _intake(store, data_root, tmp_path).scan()
    conflict = _pkg(tmp_path / "c", package_id="SYN01_OTHER_v1.0.0", extra="AREA_HA")  # aynı sürüm, farklı içerik
    with pytest.raises(UploadRejected) as exc:
        _upload(store, conflict)
    assert exc.value.code == "version_already_published"


# ----------------------------------------------------------------------------- kabul hataları

def test_missing_or_invalid_marker_is_not_processed(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    store.put_bytes(layout.announcement_key("SYN01_GHOST"), b"{}", create_only=True, operation_id="op")
    store.put_bytes(layout.announcement_key("SYN01_BAD"), b"{}", create_only=True, operation_id="op")
    store.put_bytes(layout.ready_key("SYN01_BAD"), b'{"schema": "nope"}', create_only=True, operation_id="op")
    scan = _intake(store, data_root, tmp_path).scan()
    status = {o.package_id: (o.status, o.code) for o in scan.outcomes}
    assert status == {"SYN01_GHOST": ("SKIPPED", "INCOMPLETE"), "SYN01_BAD": ("REJECTED", "marker_invalid")}
    assert list(committed_releases(store)) == []


def test_sha256_mismatch_in_cloud_is_rejected(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    server.corrupt_on_write = {"README.md"}  # depolama katmanında sessiz bozulma
    root = _pkg(tmp_path)
    _upload(store, root)
    server.corrupt_on_write = set()
    (outcome,) = _intake(store, data_root, tmp_path).scan().outcomes
    assert (outcome.status, outcome.code) == ("REJECTED", "sha256_mismatch")
    assert list(committed_releases(store)) == [] and not cloud_record_folders(data_root)
    plan = plan_package(root)
    state = json.loads(store.open_read(layout.intake_state_key(plan.package_id, plan.manifest_sha256)).read())
    assert state["status"] == "REJECTED" and state["code"] == "sha256_mismatch"
    entry = load_intake(data_root)["packages"][f"r2://{BUCKET}/{layout.package_prefix(plan.package_id)}"]
    assert entry["state"] == "FAILED" and entry["reason_code"] == "sha256_mismatch"


def test_cloud_version_conflict_and_local_duplicate_are_rejected(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    _upload(store, _pkg(tmp_path))
    _intake(store, data_root, tmp_path).scan()
    # Yükleyicinin katalog denetimini atlayan (ayrı kova senaryosu) çakışan paket, kabulde reddedilir.
    other = _pkg(tmp_path / "x", package_id="SYN01_CONFLICT", extra="AREA_HA")
    plan = plan_package(other)
    store_without_catalog_read = store
    original_head = store.head

    def head(key):
        if key.startswith(layout.CATALOG_COMMITS):
            return None
        return original_head(key)

    store.head = head
    _upload(store_without_catalog_read, other, plan=plan)
    store.head = original_head
    (outcome,) = _intake(store, data_root, tmp_path).scan().outcomes
    assert (outcome.status, outcome.code) == ("REJECTED", "VERSION_CONFLICT")
    # Yerelde kayıtlı sürüm bulutta yinelenmez.
    incoming = tmp_path / "IN"
    incoming.mkdir()
    write_ready_marker(build_package(incoming / "TT01_v1"))
    run_ingest(incoming, data_root, settle_seconds=0)
    local_dup = build_package(tmp_path / "y" / "TT01_CLOUD_COPY", package_id="TT01_CLOUD_COPY", release_overrides=PUBLIC)
    _upload(store, local_dup)
    (outcome,) = _intake(store, data_root, tmp_path).scan().outcomes
    assert outcome.status == "REJECTED" and outcome.code in ("ALREADY_REGISTERED_LOCALLY", "VERSION_CONFLICT")


def test_local_intake_refuses_version_already_committed_in_cloud(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    root = _pkg(tmp_path)
    _upload(store, root)
    _intake(store, data_root, tmp_path).scan()
    incoming = tmp_path / "IN"
    incoming.mkdir()
    import shutil

    shutil.copytree(root, incoming / "same")
    write_ready_marker(incoming / "same")
    summary = run_ingest(incoming, data_root, settle_seconds=0)
    assert [c.state for c in summary.candidates] == ["ALREADY_REGISTERED"] and summary.results == []
    assert not catalog.version_dir(data_root, DS, "1.0.0").exists()


# ----------------------------------------------------------------------------- kurtarma ve görünürlük

def test_interrupted_publication_recovers_and_stays_invisible(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    _upload(store, _pkg(tmp_path))
    original = server.put_object
    armed = {"done": False}

    def crash(**kw):
        if kw["Key"].startswith("releases/") and "/records/" in kw["Key"] and not armed["done"]:
            armed["done"] = True
            server.fail_next("put_object", "before", times=4)
        return original(**kw)

    server.put_object = crash
    (outcome,) = _intake(store, data_root, tmp_path).scan().outcomes
    server.put_object = original
    assert outcome.status == "FAILED_TRANSIENT"
    assert list(committed_releases(store)) == [] and not store.exists(layout.catalog_entry_key(DS, "1.0.0"))
    assert inspect_release(store, release_prefix(DS, "1.0.0")).status == "INCOMPLETE"
    assert not cloud_record_folders(data_root)  # yarım yayın katalogda görünmez
    (outcome,) = _intake(store, data_root, tmp_path).scan().outcomes  # "yeniden başlatılmış" işlemci
    assert outcome.status == "ACCEPTED"
    assert inspect_release(store, release_prefix(DS, "1.0.0"), full=True).status == "COMMITTED"
    assert len(cloud_record_folders(data_root)) == 1


def test_uncommitted_catalog_entry_is_never_visible(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    prefix = release_prefix(DS, "9.9.9")
    store.put_bytes(prefix + "records/version.json", b"{}", create_only=True, operation_id="op-x")
    store.put_bytes(layout.catalog_entry_key(DS, "9.9.9"), json.dumps({"dataset_id": DS, "version": "9.9.9", "operation_id": "op-x",
                                                                       "release_prefix": prefix}).encode(), create_only=True, operation_id="op-x")
    result = sync_catalog(store, data_root, provider="r2", bucket=BUCKET)
    assert result.mirrored == [] and any("release_not_committed" in p for p in result.problems)
    assert not cloud_record_folders(data_root)


def test_catalog_full_rebuild_and_incremental_sync(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    _upload(store, _pkg(tmp_path))
    _intake(store, data_root, tmp_path).scan()
    _upload(store, _pkg(tmp_path / "v2", version="1.1.0", extra="AREA_HA"))
    _intake(store, data_root, tmp_path).scan()
    versions = [v["dataset_version"] for v in contract.list_versions(data_root, f"{DS}/NEIGHBORHOODS")]
    assert versions == ["1.0.0", "1.1.0"]
    assert [v["dataset_version"] for v in contract.list_versions(data_root, f"{DS}/DISTRICTS")] == ["1.0.0"]  # bağımsız sürümleme

    import shutil

    shutil.rmtree(mirror_root(data_root).parent, onerror=lambda f, p, e: (Path(p).chmod(0o600), f(p)))
    (catalog.data_dir(data_root) / "catalog_index.sqlite").unlink()
    full = sync_catalog(store, data_root, provider="r2", bucket=BUCKET, full=True)  # yalnız işaretlerden
    assert sorted(full.mirrored) == [f"{DS}@1.0.0", f"{DS}@1.1.0"] and full.release_count == 2
    assert full.committed_bytes == sum(int(json.loads(store.open_read(p + "_COMMIT.json").read())["objects"][i]["size"])
                                       for p, c, _ in committed_releases(store) for i in range(len(c["objects"])))
    gets = server.calls["get_object"]
    inc = sync_catalog(store, data_root, provider="r2", bucket=BUCKET)
    assert inc.mirrored == [] and inc.unchanged == 2 and server.calls["get_object"] == gets  # artımlı: okuma yok
    assert load_sync_state(data_root)["last_result"] == "ok"


def test_mixed_local_and_cloud_records(cloud, tmp_path: Path, data_root: Path) -> None:
    from thermalis.data.views import catalog_items

    store, server = cloud
    incoming = tmp_path / "IN"
    incoming.mkdir()
    write_ready_marker(build_package(incoming / "TT01_v1"))
    run_ingest(incoming, data_root, settle_seconds=0)
    _upload(store, _pkg(tmp_path))
    _intake(store, data_root, tmp_path).scan()
    backends = {i["dataset_id"]: i["storage"]["backend"] for i in catalog_items(data_root)}
    assert backends == {"THERMALIS_DATA/TT01_ADMIN_BOUNDARIES/ISTANBUL": "local", DS: "r2"}
    local = contract.access(data_root, "THERMALIS_DATA/TT01_ADMIN_BOUNDARIES/ISTANBUL/NEIGHBORHOODS", authorized=True)
    assert local["storage"]["backend"] == "local" and Path(local["absolute_path"]).is_dir()
    # Aynı sürümün yansısı yerel kayıtla çakışırsa yalnız biri indekslenir ve sorun kaydı düşer.
    import shutil

    src = cloud_record_folders(data_root)[0]
    dup = mirror_root(data_root) / "TT01_ADMIN_BOUNDARIES" / "ISTANBUL" / "1.0.0-rc1"
    shutil.copytree(src, dup)
    rec = json.loads((dup / "version.json").read_text(encoding="utf-8"))
    rec.update(dataset_id="THERMALIS_DATA/TT01_ADMIN_BOUNDARIES/ISTANBUL", version="1.0.0-rc1")
    (dup / "version.json").chmod(0o600)
    (dup / "version.json").write_text(json.dumps(rec), encoding="utf-8")
    rebuild_index(data_root)
    rows = _query(data_root, "SELECT backend FROM storage_locations WHERE dataset_id=? AND version='1.0.0-rc1'",
                  ("THERMALIS_DATA/TT01_ADMIN_BOUNDARIES/ISTANBUL",))
    assert [r["backend"] for r in rows] == ["local"]
    issues = _query(data_root, "SELECT code FROM record_issues WHERE code='DUPLICATE_ACROSS_BACKENDS'")
    assert len(issues) == 1


# ----------------------------------------------------------------------------- yetki

def _authorization(root: Path, **override) -> dict:
    plan = plan_package(root)
    record = auth.template(dataset_id=plan.dataset_id, version=plan.version, package_id=plan.package_id,
                           manifest_sha256=plan.manifest_sha256, access_class=plan.access_class, provider="r2", bucket=BUCKET)
    record.update(granted_by="Test Veri Sahibi", authority="Test Kurumu", document_reference="TEST-DOC-1",
                  granted_at="2026-01-01T00:00:00Z")
    record.update(override)
    return record


def test_restricted_data_requires_valid_authorization(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    root = _pkg(tmp_path, restricted=True)
    with pytest.raises(UploadRejected) as exc:
        _upload(store, root)
    assert exc.value.code == "authorization_required"
    for override, code in (({"manifest_sha256": "0" * 64}, "authorization_does_not_match_package"),
                           ({"storage": {"provider": "r2", "bucket": "other"}}, "authorization_storage_mismatch"),
                           ({"expires_at": "2020-01-01T00:00:00Z"}, "authorization_expired"),
                           ({"scope": ["analysis"]}, "authorization_scope_missing_cloud_storage")):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(_authorization(root, **override)), encoding="utf-8")
        with pytest.raises(UploadRejected) as exc:
            _upload(store, root, authorization_path=bad)
        assert exc.value.code == code
    assert list(store.list("incoming/")) == []  # reddedilen yükleme hiçbir şey yazmadı
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_authorization(root)), encoding="utf-8")
    assert _upload(store, root, authorization_path=good)["status"] == "UPLOADED"
    # Kabul tarafı da yetkiyi bağımsız doğrular: başka kovaya yapılandırılmış işlemci reddeder.
    wrong = CloudIntake(store, store, data_root, provider="r2", bucket="another-bucket", intake_bucket=BUCKET, namespace="",
                        staging_root=tmp_path / "st2", staging_max_bytes=10 ** 10, disk_free=lambda p: 10 ** 12)
    outcome = wrong.process(plan_package(root).package_id, "etag")
    assert (outcome.status, outcome.code) == ("REJECTED", "authorization_storage_mismatch")
    assert list(committed_releases(store)) == []


def test_restricted_data_with_authorization_keeps_classification(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    root = _pkg(tmp_path, restricted=True)
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_authorization(root)), encoding="utf-8")
    _upload(store, root, authorization_path=good)
    (outcome,) = _intake(store, data_root, tmp_path).scan().outcomes
    assert outcome.status == "ACCEPTED"
    d = contract.descriptor(data_root, f"{DS}/NEIGHBORHOODS")
    assert d["access"]["classification"] == "RESTRICTED"  # sınıf değişmedi
    assert "gdal_path" not in d["storage"]  # yetkisiz istemciye konum ayrıntısı yok
    commit = json.loads(store.open_read(release_prefix(DS, "1.0.0") + "_COMMIT.json").read())
    assert any(o["key"].endswith("records/" + layout.AUTHORIZATION_NAME) for o in commit["objects"])


def test_separate_intake_bucket_isolates_uploader_from_releases(tmp_path: Path, data_root: Path) -> None:
    intake_store, intake_server = make_s3_backend(tmp_path / "intake")
    release_store, release_server = make_s3_backend(tmp_path / "release")
    root = _pkg(tmp_path, restricted=True)
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_authorization(root)), encoding="utf-8")  # yetki kalıcı (yayın) kovasını adlandırır
    _upload(intake_store, root, authorization_path=good)
    assert not release_server.objects  # yükleyici yayın kovasına hiçbir şey yazmadı
    processor = CloudIntake(intake_store, release_store, data_root, provider="r2", bucket=BUCKET, intake_bucket="thermalis-intake",
                            namespace="", staging_root=tmp_path / "staging", staging_max_bytes=10 ** 10, disk_free=lambda p: 10 ** 12)
    (outcome,) = processor.scan().outcomes
    assert outcome.status == "ACCEPTED"
    assert not any(k.startswith(("releases/", "catalog/", "state/")) for k in intake_server.objects)  # kabul kovası yalnız incoming
    assert any(k.startswith("releases/") for k in release_server.objects)
    entry = load_intake(data_root)["packages"][f"r2://thermalis-intake/{layout.package_prefix(plan_package(root).package_id)}"]
    assert entry["state"] == "REGISTERED"


def test_cloud_intake_toggle_never_changes_active_backend(tmp_path: Path, monkeypatch, capsys) -> None:
    from thermalis.cli import main
    from thermalis.settings import load_settings

    monkeypatch.setenv("THERMALIS_CONFIG_DIR", str(tmp_path / "config"))
    assert main(["storage", "cloud-intake", "on", "--interval", "600"]) == 0
    s = load_settings()
    assert (s.cloud_intake.enabled, s.cloud_intake.interval_seconds, s.storage_backend) == (True, 600, "local")
    assert main(["storage", "cloud-intake", "off"]) == 0 and load_settings().cloud_intake.enabled is False


# ----------------------------------------------------------------------------- dayanıklılık

def test_offline_scan_and_network_recovery(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    _upload(store, _pkg(tmp_path))
    server.fail_next("list_objects_v2", "before", times=4)
    processor = _intake(store, data_root, tmp_path)
    offline = processor.scan()
    assert offline.status == "offline" and offline.outcomes == []
    assert processor.scan().outcomes[0].status == "ACCEPTED"


def test_concurrent_processors_publish_exactly_once(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    _upload(store, _pkg(tmp_path))
    a = _intake(store, data_root, tmp_path, staging_root=tmp_path / "st-a")
    b = _intake(store, data_root, tmp_path, staging_root=tmp_path / "st-b")
    b.machine = "otherhost"
    results: list = []
    threads = [threading.Thread(target=lambda p=p: results.append(p.scan())) for p in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    statuses = sorted(o.status for r in results for o in r.outcomes)
    assert statuses.count("ACCEPTED") >= 1 and "REJECTED" not in statuses
    assert len(list(committed_releases(store))) == 1
    ops = {json.loads(store.open_read(p + "_COMMIT.json").read())["operation_id"] for p, _, _ in committed_releases(store)}
    assert len(ops) == 1


def test_stale_claim_is_taken_over(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    root = _pkg(tmp_path)
    _upload(store, root)
    plan = plan_package(root)
    stale = {"schema": layout.INTAKE_STATE_SCHEMA, "status": "PROCESSING", "operation_id": "int-dead", "machine": "crashed",
             "claimed_at": "2020-01-01T00:00:00Z"}
    store.put_bytes(layout.intake_state_key(plan.package_id, plan.manifest_sha256), json.dumps(stale).encode(), create_only=True,
                    operation_id="int-dead")
    fresh = dict(stale, claimed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    (outcome,) = _intake(store, data_root, tmp_path).scan().outcomes
    assert outcome.status == "ACCEPTED"
    other_root = _pkg(tmp_path / "o", package_id="SYN01_BUSY", version="2.0.0")
    _upload(store, other_root)
    p2 = plan_package(other_root)
    store.put_bytes(layout.intake_state_key(p2.package_id, p2.manifest_sha256), json.dumps(fresh).encode(), create_only=True,
                    operation_id="int-dead")
    (busy,) = _intake(store, data_root, tmp_path).scan().outcomes
    assert busy.status == "BUSY"  # taze talep başkasınındır: beklenir


def test_staging_limits_defer_without_rejecting(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    _upload(store, _pkg(tmp_path))
    (o1,) = _intake(store, data_root, tmp_path, staging_max_bytes=1024 * 1024).scan().outcomes
    assert (o1.status, o1.code) == ("DEFERRED", "insufficient_staging_space")
    (o2,) = _intake(store, data_root, tmp_path, disk_free=lambda p: 1024).scan().outcomes
    assert o2.status == "DEFERRED" and list(committed_releases(store)) == []
    (o3,) = _intake(store, data_root, tmp_path).scan().outcomes
    assert o3.status == "ACCEPTED"


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows yol sınırı")
def test_windows_path_limit_defers_instead_of_failing(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    _upload(store, _pkg(tmp_path))
    deep = tmp_path / ("d" * 45) / ("e" * (190 - len(str(tmp_path)) - 47))  # klasör oluşturulabilir, payload yolları > 250
    (outcome,) = _intake(store, data_root, tmp_path, staging_root=deep).scan().outcomes
    assert (outcome.status, outcome.code) == ("DEFERRED", "staging_path_too_long")
    assert list(committed_releases(store)) == []
    (outcome,) = _intake(store, data_root, tmp_path).scan().outcomes  # kısa hazırlık klasörüyle aynı paket kabul edilir
    assert outcome.status == "ACCEPTED"


def test_local_environment_error_is_not_a_permanent_rejection(cloud, tmp_path: Path, data_root: Path, monkeypatch) -> None:
    import thermalis.data.ingest as ingest

    store, server = cloud
    root = _pkg(tmp_path)
    _upload(store, root)
    monkeypatch.setattr(ingest, "ingest_path", lambda *a, **k: {"state": "FAILED", "reason_code": "unexpected",
                                                                 "reason": "UNEXPECTED: OSError: disk"})
    (outcome,) = _intake(store, data_root, tmp_path).scan().outcomes
    assert (outcome.status, outcome.code) == ("DEFERRED", "unexpected")
    plan = plan_package(root)
    state = json.loads(store.open_read(layout.intake_state_key(plan.package_id, plan.manifest_sha256)).read())
    assert state["status"] == "PROCESSING"  # kalıcı RED yazılmadı
    monkeypatch.undo()
    assert _intake(store, data_root, tmp_path).scan().outcomes[0].status == "ACCEPTED"


def test_scan_lock_prevents_parallel_local_processors(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "cloud-intake.lock").write_text("123", encoding="utf-8")
    assert _intake(store, data_root, tmp_path).scan().status == "busy"


# ----------------------------------------------------------------------------- doğrulama, günlük, API

def test_cloud_version_reverification_detects_tampering(cloud, tmp_path: Path, data_root: Path) -> None:
    store, server = cloud
    _upload(store, _pkg(tmp_path))
    _intake(store, data_root, tmp_path).scan()
    assert all(c.status == "PASS" for c in verify_cloud_version(store, data_root, DS, "1.0.0"))
    key = next(k for k in server.objects if k.startswith("releases/") and k.endswith(".gdbtable"))
    body = server.objects[key]["body"]
    server.objects[key]["body"] = body[:-1] + bytes([body[-1] ^ 1])
    assert verify_cloud_version(store, data_root, DS, "1.0.0")[0].status == "FAIL"


def test_local_watcher_logging_is_deduplicated(tmp_path: Path, data_root: Path) -> None:
    incoming = tmp_path / "IN"
    incoming.mkdir()
    write_ready_marker(build_package(incoming / "TT01_v1"))
    (incoming / "loose.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 32)
    for _ in range(4):
        run_ingest(incoming, data_root, settle_seconds=0, retry_failed=False)
    events = (catalog.data_dir(data_root) / "_ingest" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    discoveries = [json.loads(e) for e in events if json.loads(e)["event"] == "discovery"]
    assert len(discoveries) == 2  # 1: ilk keşif, 2: kayıt sonrası (REGISTERED → ALREADY_REGISTERED); sonra sessiz
    path = catalog.data_dir(data_root) / "_ingest" / "intake.json"
    before = path.stat().st_mtime_ns
    run_ingest(incoming, data_root, settle_seconds=0, retry_failed=False)
    assert path.stat().st_mtime_ns == before  # değişiklik yoksa durum dosyası yeniden yazılmaz


def test_storage_status_api_never_exposes_secrets_or_endpoint(tmp_path: Path, monkeypatch, data_root: Path) -> None:
    from fastapi.testclient import TestClient

    from thermalis.app import create_app
    from thermalis.settings import CacheSettings, CloudSettings, Settings, save_settings

    monkeypatch.setenv("THERMALIS_CONFIG_DIR", str(tmp_path / "config"))
    save_settings(Settings(data_root=str(data_root), cache=CacheSettings(cache_dir=str(tmp_path / "cache")),
                           cloud=CloudSettings(endpoint="https://0123abcdSECRETACCOUNT.r2.cloudflarestorage.com", bucket=BUCKET)))
    client = TestClient(create_app(watch=False))
    body = client.get("/api/data/storage").json()
    text = json.dumps(body)
    assert "0123abcdSECRETACCOUNT" not in text and "cloudflarestorage" not in text
    assert body["active_backend"] == "local" and body["cloud"]["configured"] is True and body["cloud"]["intake_enabled"] is False
    assert body["catalog"]["committed_bytes_source"] == "catalog_derived" and body["billing_note"] == "provider_billing_not_queried"
    assert body["cache"]["max_bytes"] == 20 * 1024 ** 3
    assert client.post("/api/data/storage/sync").json() == {"triggered": True}
    assert client.get("/api/data/intake").json()["watcher"]["cloud"]["enabled"] is False


def test_watcher_cloud_scan_is_bounded_and_reports_status(tmp_path: Path, data_root: Path, cloud) -> None:
    from thermalis.data.intake import IntakeWatcher
    from thermalis.settings import CloudIntakeSettings, CloudSettings, Settings

    store, server = cloud
    _upload(store, _pkg(tmp_path))
    settings = Settings(data_root=str(data_root), cloud=CloudSettings(endpoint="https://x.r2.cloudflarestorage.com", bucket=BUCKET),
                        cloud_intake=CloudIntakeSettings(enabled=True, interval_seconds=3600))
    watcher = IntakeWatcher(lambda: settings, cloud_factory=lambda s, root: _intake(store, root, tmp_path))
    watcher.cloud_scan_if_due()
    assert watcher.cloud.last_result == "ok" and watcher.cloud.last_outcomes[0]["status"] == "ACCEPTED"
    lists = server.calls["list_objects_v2"]
    assert watcher.cloud_scan_if_due() > 3000 and server.calls["list_objects_v2"] == lists  # aralık dolmadan istek yok
    watcher.trigger(cloud=True)
    watcher.cloud_scan_if_due()
    assert server.calls["list_objects_v2"] > lists  # "şimdi eşitle"
    disabled = IntakeWatcher(lambda: Settings(data_root=str(data_root)))
    disabled.cloud_scan_if_due()
    assert disabled.cloud.last_result == "disabled"
