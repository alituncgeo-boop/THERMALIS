"""Bilimsel ve işletimsel sağlamlaştırma: bulut defteri, metadata düzeltmeleri, operatör yetkileri, sürüm başına
yetkili kaynak (geçiş + geri alma), zaman modeli, GDB domain ayrıntıları ve boş veri kökünden yeniden kurulum (sahte R2)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from s3fake import make_s3_backend
from thermalis.cloud import audit_logs, grants, ledger
from thermalis.cloud.authority import AuthorityError, history, load_authority, set_authority, verify_equivalence
from thermalis.cloud.authorization import AuthorizationError
from thermalis.cloud.catalog_sync import sync_catalog
from thermalis.cloud.e2e import prepare_legacy_release
from thermalis.cloud.migrate import MigrationRefused, migrate_release, plan_migration
from thermalis.data import catalog, contract, revisions, temporal
from thermalis.data.catalog_index import _query

BUCKET = "thermalis-test-bucket"
DS = "THERMALIS_DATA/SYN95_HARDEN/TESTKENT"
LID = f"{DS}/NEIGHBORHOODS"
RDS = "THERMALIS_DATA/SYN94_HARDEN/TESTKENT"


def _tree_hashes(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture()
def legacy(tmp_path: Path) -> Path:
    root = tmp_path / "THERMALIS_DATA"
    root.mkdir()
    prepare_legacy_release(tmp_path, root, dataset_id=DS, package_id="SYN95_HARDEN_v1.0.0", restricted=False)
    return root


@pytest.fixture()
def migrated(legacy: Path, tmp_path: Path):
    store, server = make_s3_backend(tmp_path)
    plan = plan_migration(legacy, DS, "1.0.0", provider="r2", bucket=BUCKET)
    assert migrate_release(store, plan)["status"] == "MIGRATED"
    assert sync_catalog(store, legacy, provider="r2", bucket=BUCKET).status == "ok"
    return legacy, store, server


def _grant(**over):
    spec = {"granted_by": "Data Owner (test)", "attestation": "Operator attestation of institutional authority (test)",
            "attestation_date": "2026-11-27", "operations": ["cloud_storage", "transfer", "thermalis_data_registration"],
            "destinations": [{"provider": "r2", "bucket": BUCKET}],
            "datasets": [{"dataset_id": RDS, "version": "1.0.0"}], "conditions": ["no public redistribution"]}
    spec.update(over)
    return grants.build_grant(**spec)


# --------------------------------------------------------------------------- defter

def test_ledger_is_create_only_and_content_addressed(tmp_path: Path, legacy: Path) -> None:
    store, _ = make_s3_backend(tmp_path)
    body = {"schema": "x/1", "value": 1}
    body["id"] = "e-" + ledger.content_id(body)
    ledger.record_local(legacy, "authorizations", body["id"], body)
    ledger.record_local(legacy, "authorizations", body["id"], body)  # aynı içerik: sorun değil
    with pytest.raises(ledger.LedgerError):
        ledger.record_local(legacy, "authorizations", body["id"], {**body, "value": 2})
    assert ledger.push_pending(store, legacy) == {"pushed": 1, "conflicts": 0}
    assert ledger.push(store, "authorizations", body["id"], body) in ("written", "exists")  # eşgüçlü
    with pytest.raises(ledger.LedgerError):
        ledger.push(store, "authorizations", body["id"], {**body, "value": 3})  # bulutta üzerine yazılmaz
    with pytest.raises(ledger.LedgerError):
        ledger.record_local(legacy, "authorizations", "../escape", body)


# --------------------------------------------------------------------------- operatör yetkileri

def test_grant_scope_attestation_expiry_revocation_and_tamper(legacy: Path) -> None:
    with pytest.raises(grants.GrantError):
        _grant(datasets=[{"dataset_id": "THERMALIS_DATA/*", "version": "1.0.0"}])  # joker yok
    with pytest.raises(grants.GrantError):
        _grant(operations=["publish_publicly"])
    with pytest.raises(grants.GrantError):
        _grant(evidence_type="institutional_document")  # belge türü kanıt belge referansı ister
    g = _grant()
    assert g["document_reference"] is None and g["evidence_type"] == "operator_attestation"  # beyan yeterli
    grants.save(legacy, g)
    kw = dict(dataset_id=RDS, version="1.0.0", provider="r2", bucket=BUCKET)
    assert grants.find(legacy, operation="cloud_storage", **kw)["id"] == g["id"]
    assert grants.find(legacy, operation="scientific_analysis_internal", **kw) is None  # kapsam dışı işlem
    assert grants.find(legacy, operation="cloud_storage", **{**kw, "bucket": "other"}) is None  # başka hedef
    assert grants.find(legacy, operation="cloud_storage", **{**kw, "dataset_id": DS}) is None  # başka veri seti
    expired = _grant(expires_at="2000-01-01T00:00:00Z", created_at="1999-01-01T00:00:00Z")
    assert grants.status(expired, set()) == "expired"
    tampered = {**g, "datasets": g["datasets"] + [{"dataset_id": DS, "version": "1.0.0"}]}  # kapsam genişletme denemesi
    assert grants.status(tampered, set()) == "tampered"
    grants.save(legacy, grants.build_revocation(g["id"], revoked_by="owner", reason="test"))
    assert grants.find(legacy, operation="cloud_storage", **kw) is None
    assert grants.status(g, grants.all_grants(legacy)[1]) == "revoked"


def test_restricted_migration_reuses_grant_without_new_record(legacy: Path, tmp_path: Path) -> None:
    prepare_legacy_release(tmp_path / "r", legacy, dataset_id=RDS, package_id="SYN94_HARDEN_v1.0.0", restricted=True)
    with pytest.raises(MigrationRefused) as exc:
        plan_migration(legacy, RDS, "1.0.0", provider="r2", bucket=BUCKET)
    assert exc.value.code == "authorization_required"
    g = _grant(datasets=[{"dataset_id": RDS, "version": "1.0.0"}])
    grants.save(legacy, g)
    plan = plan_migration(legacy, RDS, "1.0.0", provider="r2", bucket=BUCKET)  # yeniden onay istenmez
    record = json.loads(plan.records["THERMALIS_AUTHORIZATION.json"])
    assert record["grant_id"] == g["id"] and record["evidence_type"] == "operator_attestation"
    assert record["access_class"] == "RESTRICTED" and record["document_reference"] is None
    assert "no public redistribution" in record["conditions"]
    migration = json.loads(plan.records["migration.json"])
    assert migration["access_class_changed"] is False


def test_legacy_authorization_record_with_document_reference_stays_valid() -> None:
    from thermalis.cloud.authorization import SCHEMA, validate

    rec = {"schema": SCHEMA, "dataset_id": RDS, "version": "1.0.0", "package_id": "P", "manifest_sha256": "a" * 64,
           "access_class": "RESTRICTED", "scope": ["cloud_storage"], "storage": {"provider": "r2", "bucket": BUCKET},
           "granted_by": "x", "authority": "y", "document_reference": "DOC-1", "granted_at": "2026-01-01T00:00:00Z"}
    kw = dict(dataset_id=RDS, version="1.0.0", package_id="P", manifest_sha256="a" * 64, access_class="RESTRICTED",
              provider="r2", bucket=BUCKET)
    assert validate(rec, **kw) is rec
    assert validate({k: v for k, v in rec.items() if k != "document_reference"}, **kw)  # belge artık isteğe bağlı
    with pytest.raises(AuthorizationError):
        validate({**rec, "storage": {"provider": "r2", "bucket": "public-bucket"}}, **kw)


# --------------------------------------------------------------------------- metadata düzeltmeleri

def test_revision_overlays_without_touching_committed_records(migrated) -> None:
    root, store, _ = migrated
    from thermalis.cloud.catalog_sync import mirror_folder

    mirror = mirror_folder(root, DS, "1.0.0")
    before = _tree_hashes(mirror)
    with pytest.raises(revisions.RevisionError):
        revisions.create(root, dataset_id=DS, version="1.0.0", target="r2", reason="r", evidence=[{"source": "x"}], created_by="t",
                         changes=[{"record": "version.json", "path": "access.classification", "op": "set", "value": "PUBLIC"}])
    with pytest.raises(revisions.RevisionError):
        revisions.create(root, dataset_id=DS, version="1.0.0", target="r2", reason="", evidence=[], created_by="t",
                         changes=[{"record": "version.json", "path": "source.note", "op": "set", "value": 1}])
    availability = {"reference_geography": "HD01", "missing_semantics": "NULL_NO_DATA_NOT_ZERO",
                    "zero_semantics": "REAL_OBSERVED_ZERO", "coverage": {"matched": 2}}
    rev = revisions.create(root, dataset_id=DS, version="1.0.0", target="r2", reason="coverage", created_by="tester",
                           evidence=[{"source": "PACKAGE/X.json"}],
                           changes=[{"record": "version.json", "path": "layers.Neighborhoods.data_availability", "op": "set",
                                     "value": availability}])
    assert rev["changes"][0]["previous"] is None and rev["base_operation_id"] == revisions.base_operation(mirror)
    set_authority(root, DS, "1.0.0", "r2", decided_by="tester", reason="cutover", store=store)
    assert contract.access(root, LID, authorized=True)["data_availability"] == availability
    desc = contract.descriptor(root, LID)
    assert [r["revision_id"] for r in desc["metadata_revisions"]] == [rev["id"]] and desc["metadata_revisions"][0]["applied"]
    assert _tree_hashes(mirror) == before  # commit'li kayıt baytları değişmedi
    # Başka temel işleme bağlı düzeltme uygulanmaz; içeriği değiştirilmiş girdi yok sayılır.
    other = revisions.build(dataset_id=DS, version="1.0.0", base_operation_id="op-other", records={}, reason="x",
                            evidence=[{"source": "y"}], created_by="t",
                            changes=[{"record": "version.json", "path": "layers.Neighborhoods.data_availability", "op": "set", "value": "X"}])
    ledger.record_local(root, "metadata_revisions", other["id"], other)
    forged = {**rev, "reason": "changed later"}
    path = ledger.ledger_dir(root, "metadata_revisions") / f"{rev['id']}.json"
    path.write_text(json.dumps(forged), encoding="utf-8")
    from thermalis.data.catalog_index import rebuild_index

    rebuild_index(root)
    assert contract.access(root, LID, authorized=True)["data_availability"] is None
    path.write_text(json.dumps({k: v for k, v in rev.items()}), encoding="utf-8")
    rebuild_index(root)
    assert contract.access(root, LID, authorized=True)["data_availability"] == availability


# --------------------------------------------------------------------------- yetkili kaynak (geçiş + geri alma)

def test_authority_switch_requires_equivalence_and_supports_rollback(migrated, monkeypatch) -> None:
    root, store, _ = migrated
    import thermalis.cloud.authority as authority_module

    monkeypatch.setattr(authority_module, "_now", lambda: "2026-11-27T12:00:00Z")  # aynı saniyede geçiş + geri alma
    local = catalog.version_dir(root, DS, "1.0.0")
    before = _tree_hashes(local)
    cid_local = contract.descriptor(root, LID)["content"]["content_id"]
    with pytest.raises(AuthorityError):
        set_authority(root, DS, "1.0.0", "r2", decided_by="t", reason="no read-back", store=None)
    checks = verify_equivalence(root, DS, "1.0.0", store=store)
    assert [c["status"] for c in checks] == ["PASS"] * len(checks), checks
    decision = set_authority(root, DS, "1.0.0", "r2", decided_by="tester", reason="cutover", store=store)
    assert decision["previous_source"] == "local" and load_authority(root)[f"{DS}@1.0.0"]["source"] == "r2"
    assert [r["backend"] for r in _query(root, "SELECT backend FROM storage_locations WHERE dataset_id=?", (DS,))] == ["r2"]
    assert len(_query(root, "SELECT 1 FROM dataset_versions WHERE dataset_id=?", (DS,))) == 1  # çift kayıt yok
    desc = contract.descriptor(root, LID)
    assert desc["storage"]["backend"] == "r2" and desc["content"]["content_id"] == cid_local
    assert {(c["backend"], c["role"]) for c in desc["copies"]} == {("r2", "authoritative"), ("local", "secondary")}
    access = contract.access(root, LID, authorized=True)
    assert access["absolute_path"] is None and access["materialize"]
    assert contract.quality(root, LID)["special_records"]  # GLOBALID'li özel kayıtlar korunur
    assert contract.field_info(root, LID, None, "YAKA")["status"] == "USER_VERIFIED"  # anlamsal doğrulama geçerli
    assert _tree_hashes(local) == before  # yerel sürüm klasörü dokunulmadı
    set_authority(root, DS, "1.0.0", "local", decided_by="tester", reason="rollback")
    assert [r["backend"] for r in _query(root, "SELECT backend FROM storage_locations WHERE dataset_id=?", (DS,))] == ["local"]
    assert contract.access(root, LID, authorized=True)["absolute_path"]
    assert [d["source"] for d in history(root, DS, "1.0.0")] == ["r2", "local"]  # geçmiş silinmez


def test_authority_refused_without_cloud_copy(legacy: Path, tmp_path: Path) -> None:
    store, _ = make_s3_backend(tmp_path)
    with pytest.raises(AuthorityError) as exc:
        set_authority(legacy, DS, "1.0.0", "r2", decided_by="t", reason="x", store=store)
    assert exc.value.code == "authority_equivalence_failed"
    assert not load_authority(legacy)


# --------------------------------------------------------------------------- boş veri kökünden yeniden kurulum

def test_empty_data_root_rebuilds_catalog_history_and_decisions_from_cloud(migrated, tmp_path: Path) -> None:
    root, store, server = migrated
    grants.save(root, _grant())
    revisions.create(root, dataset_id=DS, version="1.0.0", target="r2", reason="vertical", created_by="tester",
                     evidence=[{"source": "PACKAGE/CRS_AND_UNITS.json"}],
                     changes=[{"record": "version.json", "path": "source.note_test", "op": "set", "value": "ok"}])
    set_authority(root, DS, "1.0.0", "r2", decided_by="tester", reason="cutover", store=store)
    assert ledger.safe_sync(store, root)["status"] == "ok"
    rever = (catalog.data_dir(root) / "_ingest" / "verifications.jsonl").read_text(encoding="utf-8").splitlines()
    sem_local = (catalog.data_dir(root) / "_semantic" / "verifications.jsonl").read_text(encoding="utf-8").splitlines()

    fresh = tmp_path / "EMPTY_ROOT"
    fresh.mkdir()
    fetched: list[str] = []
    original = server.get_object

    def logging_get(**kw):
        fetched.append(kw["Key"])
        return original(**kw)

    server.get_object = logging_get
    result = sync_catalog(store, fresh, provider="r2", bucket=BUCKET, full=True)
    server.get_object = original
    assert result.status == "ok" and result.ledger["status"] == "ok", result
    # Anlamsal doğrulama zinciri ve yeniden doğrulama geçmişi buluttan aynen geri kuruldu (zincir denetimiyle).
    assert (catalog.data_dir(fresh) / "_semantic" / "verifications.jsonl").read_text(encoding="utf-8").splitlines() == sem_local
    restored = (catalog.data_dir(fresh) / "_ingest" / "verifications.jsonl").read_text(encoding="utf-8").splitlines()
    key = lambda line: json.dumps(json.loads(line), sort_keys=True)  # noqa: E731 - içerik aynı, anahtar sırası kanonik
    assert sorted(map(key, restored)) == sorted(map(key, rever)) and rever
    assert contract.field_info(fresh, LID, None, "YAKA")["status"] == "USER_VERIFIED"
    assert contract.descriptor(fresh, LID)["metadata_revisions"][0]["applied"]
    assert grants.find(fresh, dataset_id=RDS, version="1.0.0", operation="cloud_storage", provider="r2", bucket=BUCKET)
    assert load_authority(fresh)[f"{DS}@1.0.0"]["source"] == "r2"
    assert contract.descriptor(fresh, LID)["content"]["content_id"] == contract.descriptor(root, LID)["content"]["content_id"]
    # Yük (payload) dosyaları indirilmez: yansıda yalnız kayıtlar ve küçük JSON belgeleri bulunur.
    mirrored = [p for p in (catalog.data_dir(fresh) / "_cloud").rglob("*") if p.is_file()]
    assert not [p for p in mirrored if p.suffix in (".gdbtable", ".gdbtablx", ".atx", ".tif", ".zip")]
    payload_reads = [k for k in fetched if "/payload/" in k and not k.endswith(".json")]
    assert fetched and payload_reads == []  # yalnız işaretler, kayıtlar, belge JSON'ları ve defter okundu


def test_semantic_log_divergence_is_reported_and_local_log_untouched(migrated, tmp_path: Path) -> None:
    root, store, _ = migrated
    assert ledger.safe_sync(store, root)["status"] == "ok"
    other = tmp_path / "OTHER"
    other.mkdir()
    path = catalog.data_dir(other) / "_semantic" / "verifications.jsonl"
    path.parent.mkdir(parents=True)
    from thermalis.data import semantics

    semantics.append_verification(other, {"dataset_id": LID, "version": "1.0.0", "field": "YAKA", "meaning": "different",
                                          "verified_by": "other"}, content_id="x", auth_method="in_process")
    original = path.read_bytes()
    ledger.pull(store, other)
    out = audit_logs.restore_local_logs(other)
    assert out["semantic_conflict"] and out["semantic_restored"] == 0
    assert path.read_bytes() == original


# --------------------------------------------------------------------------- zaman modeli

def test_temporal_model_keeps_meaning_and_unknowns() -> None:
    hd02 = {"source": {"temporal_coverage": {"first_acquisition": "2013-05-01", "last_acquisition": "2024-08-30", "item_count": 12,
                                             "basis": "collection items"}}}
    e = temporal.entries(hd02)
    assert e[0]["kind"] == "acquisition_date" and e[0]["continuity"] == "selected_acquisitions" and e[0]["status"] == "SOURCE_DATA"
    explicit = {"source": {"temporal_entries": [
        {"kind": "demographic_reference_year", "start": "2021", "continuity": "single_date", "status": "SOURCE_DECLARED"},
        {"kind": "observation_period"}]},
        "registration": {"registered_at": "2026-11-27T10:00:00Z"}}
    e = temporal.entries(explicit)
    assert e[1]["status"] == "UNKNOWN" and e[1]["start"] is None  # kayıt tarihi yerine konmaz
    assert temporal.summary_state(e) == "partial"
    legacy = {"source": {"temporal_coverage": {"semantics": "observation_period", "start": "1991-01-01", "end": "2020-12-31",
                                               "basis": "data:time"}}}
    e = temporal.entries(legacy)
    assert e[0]["kind"] == "observation_period" and e[0]["continuity"] == "continuous" and e[0]["status"] == "SOURCE_DATA"
    assert temporal.entries({"source": {}}) == []


# --------------------------------------------------------------------------- GDB domainleri (genel; kaynak paketleri)

def test_bundle_domains_are_extracted_with_status_and_assessment(tmp_path: Path, data_root: Path) -> None:
    from bundlefake import build_bundle, bundle_declaration
    from thermalis.cloud.direct import plan_collection, publish_collection

    bundle = build_bundle(tmp_path / "in")
    store, _ = make_s3_backend(tmp_path)
    plan = plan_collection(bundle, bundle_declaration(bundle), data_root=data_root, work_root=tmp_path / "work", provider="r2",
                           bucket=BUCKET)
    assert publish_collection(store, plan, machine="m1")["status"] == "PUBLISHED"
    assert sync_catalog(store, data_root, provider="r2", bucket=BUCKET).status == "ok"
    ds = plan.dataset_id
    details = _query(data_root, "SELECT * FROM domain_details WHERE dataset_id=?", (ds,))
    assert details and all(d["origin"] == "extracted_from_source_geodatabase" for d in details)
    assert all(d["semantic_status"] == "EXTRACTED_NOT_SCIENTIFICALLY_VERIFIED" for d in details)  # çıkarıldı ≠ doğrulandı
    assert all(d["domain_kind"] == "coded" for d in details)
    assessed = [json.loads(d["unexpected_codes"]) for d in details if d["unexpected_codes"].startswith("{")]
    assert assessed and all(a["status"] in ("ALL_CODES_DEFINED", "UNEXPECTED_CODES_PRESENT") for a in assessed)
    lid = f"{ds}/NEIGHBORHOODS"
    sem = contract.semantic_dictionary(data_root, lid)
    coded = [f for f in sem["fields"] if f["domain_name"]]
    assert coded and all(f["domain_semantic_status"] == "EXTRACTED_NOT_SCIENTIFICALLY_VERIFIED" for f in coded)
    codes = contract.domain_codes(data_root, lid, None, coded[0]["domain_name"])
    assert codes["detail"]["domain_kind"] == "coded" and codes["codes"]


def test_hd01_style_package_domains_remain_compatible(legacy: Path) -> None:
    sem = contract.semantic_dictionary(legacy, LID)
    yaka = next(f for f in sem["fields"] if f["field"] == "YAKA")
    codes = contract.domain_codes(legacy, LID, None, yaka["domain_name"])
    assert codes["codes"] and codes["detail"]["origin"] == "package_declaration_matches_source_geodatabase"
