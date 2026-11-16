"""Anlamsal veri sözlüğü, domain sorgusu, veri erişim sözleşmesi (v1) ve erişim koruması (sentetik paketlerle)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import build_package, write_ready_marker
from thermalis.data import catalog, contract, semantics
from thermalis.data.catalog_index import rebuild_index
from thermalis.data.ingest import run_ingest
from thermalis.data.integrity import sha256_file
from thermalis.settings import Settings, save_settings

DS = "THERMALIS_DATA/TT01_ADMIN_BOUNDARIES/ISTANBUL"
N = f"{DS}/NEIGHBORHOODS"
D = f"{DS}/DISTRICTS"
PASSPORT = {
    "identity": {"theme": ["administrative_boundaries"], "geographic_scope": "Testkent, TR"},
    "spatial_temporal": {"temporal_currency_year": 2026},
    "contents": {
        "feature_classes": [
            {"layer_name": "Districts", "role": "administrative_district_boundaries",
             "field_semantics": {"KIMLIKNO": "Unique district identifier; joins Neighborhoods.ILCEKN", "AD": "District name"}},
            {"layer_name": "Neighborhoods", "role": "administrative_neighborhood_and_special_area_boundaries",
             "field_semantics": {"ILCEKN": "Foreign key referencing Districts.KIMLIKNO", "KIMLIKNO": "Official neighborhood ID when applicable",
                                 "AD": "Name of neighborhood or special area", "Shape_Area": "Stored source area field"}},
        ],
        "join_relationships": [{"from": "Neighborhoods.ILCEKN", "to": "Districts.KIMLIKNO", "kind": "many_to_one",
                                "cardinality_verified": True, "evidence": "EVIDENCE/JOIN.json"}],
    },
}


@pytest.fixture()
def incoming(tmp_path: Path) -> Path:
    folder = tmp_path / "INCOMING"
    folder.mkdir()
    return folder


def _register(incoming: Path, data_root: Path, name: str, **kwargs) -> None:
    write_ready_marker(build_package(incoming / name, **kwargs))
    summary = run_ingest(incoming, data_root, settle_seconds=0)
    assert all(r.technical == "PASS" for r in summary.results), [r.stop_reason for r in summary.results]


def _client(tmp_path: Path, monkeypatch, data_root: Path) -> TestClient:
    from thermalis.app import create_app

    monkeypatch.setenv("THERMALIS_CONFIG_DIR", str(tmp_path / "config"))
    save_settings(Settings(data_root=str(data_root)))
    return TestClient(create_app(watch=False))


def _field(data_root: Path, dataset: str, name: str, version: str | None = None) -> dict:
    return next(f for f in contract.semantic_dictionary(data_root, dataset, version)["fields"] if f["field"] == name)


# ----------------------------------------------------------------------------- anlamsal sözlük

def test_source_declared_unknown_and_inferred_are_distinguished(incoming: Path, data_root: Path) -> None:
    _register(incoming, data_root, "TT01_v1", passport=PASSPORT)
    ilcekn = _field(data_root, N, "ILCEKN")
    assert (ilcekn["status"], ilcekn["origin"]) == (semantics.SOURCE_DECLARED, "package_passport")
    assert ilcekn["meaning"] == "Foreign key referencing Districts.KIMLIKNO" and ilcekn["source_ref"].endswith("field_semantics.ILCEKN")
    assert ilcekn["domain_name"] == "Ilce_UAVT" and ilcekn["domain_origin"] == "gdb_field_domain"
    yaka = _field(data_root, N, "YAKA")  # tipi bilinen ama kaynakta anlamı yazılmamış alan
    assert (yaka["status"], yaka["meaning"], yaka["data_type"]) == (semantics.UNKNOWN, None, "Integer")
    area = _field(data_root, N, "Shape_Area")  # anlam kaynakta, birim biçim kuralından önerilir: ayrı durumlar
    assert area["status"] == semantics.SOURCE_DECLARED
    assert (area["unit"], area["unit_status"]) == ("m²", semantics.INFERRED_UNVERIFIED) and "format_rule" in area["unit_origin"]
    kim = _field(data_root, N, "KIMLIKNO")
    assert kim["null_meaning"] == "DOCUMENTED_SPECIAL_RECORDS:1" and kim["null_meaning_status"] == semantics.SOURCE_DECLARED
    assert kim["null_count"] == 1
    assert _field(data_root, N, "AD")["scale"] is None  # uygulanmayan öznitelikler zorunlu değildir


def test_no_passport_means_everything_unknown_not_invented(incoming: Path, data_root: Path) -> None:
    _register(incoming, data_root, "bare")
    fields = {f["field"]: f for f in contract.semantic_dictionary(data_root, N)["fields"]}
    # OBJECTID/Shape teknik alanlardır: türleri kaynak sözlükte bildirilir, bilimsel öznitelik eksikliği sayılmaz.
    for name in ("OBJECTID", "Shape"):
        assert fields[name]["field_role"] == "structural" and fields[name]["status"] == semantics.SOURCE_DECLARED
    attributes = [f for f in fields.values() if f["field_role"] == "attribute"]
    assert attributes and {f["status"] for f in attributes} == {semantics.UNKNOWN}
    assert all(f["meaning"] is None for f in attributes)
    completeness = {c["item"]: c for c in contract.descriptor(data_root, N)["metadata_completeness"]}
    assert completeness["semantic_definitions"]["state"] == "missing" and completeness["semantic_definitions"]["group"] == "semantic"


def test_user_verification_persists_across_index_rebuild_and_restart(incoming: Path, data_root: Path, tmp_path: Path, monkeypatch) -> None:
    _register(incoming, data_root, "TT01_v1", passport=PASSPORT)
    client = _client(tmp_path, monkeypatch, data_root)
    from thermalis.settings import component_token

    auth = {"X-Thermalis-Component-Token": component_token()}
    body = {"dataset_id": N, "version": "1.0.0-rc1", "field": "YAKA", "meaning": "Side of the Bosphorus (coded)", "verified_by": "tester"}
    r = client.post("/api/data/v1/semantics/verify", json=body, headers=auth)
    assert r.status_code == 200 and r.json()["status"] == semantics.USER_VERIFIED and r.json()["verified_by"] == "tester"
    assert client.post("/api/data/v1/semantics/verify", json={**body, "field": "NOPE"}, headers=auth).status_code == 404
    assert client.post("/api/data/v1/semantics/verify", json={k: body[k] for k in ("dataset_id", "version", "field")}, headers=auth).status_code == 400
    catalog_index = catalog.data_dir(data_root) / "catalog_index.sqlite"
    catalog_index.unlink()  # dizin türetilmiştir: silinip yeniden kurulsa bile doğrulama kalır
    rebuild_index(data_root)
    again = _client(tmp_path, monkeypatch, data_root)  # "yeniden başlatılmış" uygulama
    yaka = again.get("/api/data/v1/field", params={"id": N, "name": "YAKA"}).json()
    assert (yaka["status"], yaka["meaning"]) == (semantics.USER_VERIFIED, "Side of the Bosphorus (coded)")
    final = catalog.version_dir(data_root, DS, "1.0.0-rc1")
    assert "Bosphorus" not in (final / "version.json").read_text(encoding="utf-8")  # değişmez sürüm kaydına yazılmadı


def test_new_version_carries_verified_meaning_only_as_unverified_proposal(incoming: Path, data_root: Path, tmp_path: Path, monkeypatch) -> None:
    _register(incoming, data_root, "TT01_v1", passport=PASSPORT)
    bound = contract.field_info(data_root, N, "1.0.0-rc1", "YAKA")["content_id"]
    assert bound and bound == contract.semantic_dictionary(data_root, N)["content_id"]
    semantics.append_verification(data_root, {"dataset_id": N, "version": "1.0.0-rc1", "field": "YAKA", "meaning": "Side code", "unit": "-"},
                                  content_id=bound)
    _register(incoming, data_root, "TT01_v2", passport=PASSPORT, version="1.0.0", extra_field="AREA_HA")
    old, new = _field(data_root, N, "YAKA", "1.0.0-rc1"), _field(data_root, N, "YAKA", "1.0.0")
    assert old["status"] == semantics.USER_VERIFIED  # önceki sürüm bilgisi kaybolmaz
    assert (new["status"], new["meaning"], new["origin"], new["proposal_compatibility"]) == (
        semantics.INFERRED_UNVERIFIED, "Side code", "carried_from:1.0.0-rc1", "compatible")  # otomatik kesinleşmez
    diff = contract.diff(data_root, N, "1.0.0-rc1", "1.0.0")
    kinds = {(c["field"], c["change"]) for c in diff["field_changes"]}
    assert ("AREA_HA", "added") in kinds and ("YAKA", "semantic_status_changed") in kinds and not diff["unchanged"]
    assert contract.diff(data_root, D, "1.0.0-rc1", "1.0.0")["unchanged"] is True  # değişmeyen katman ayırt edilir
    assert [v["version"] for v in contract.list_versions(data_root, N)] == ["1.0.0-rc1", "1.0.0"]
    assert [v["version"] for v in contract.list_versions(data_root, D)] == ["1.0.0-rc1"]  # Districts yeni sürüm almadı


# ----------------------------------------------------------------------------- domainler

def test_coded_domains_are_queryable_from_a_field(incoming: Path, data_root: Path, tmp_path: Path, monkeypatch) -> None:
    _register(incoming, data_root, "TT01_v1", passport=PASSPORT)
    client = _client(tmp_path, monkeypatch, data_root)
    f = client.get("/api/data/v1/field", params={"id": N, "name": "ILCEKN"}).json()
    assert f["domain"]["name"] == "Ilce_UAVT" and f["domain"]["defined_in_source"] is True and f["domain"]["code_count"] == 2
    codes = client.get(f["domain"]["link"]).json()
    assert {c["code"]: c["label"] for c in codes["codes"]} == {"1103": "ADALAR", "1421": "KADIKÖY"}  # kaynakla birebir
    assert codes["used_by_fields"] == [{"layer_dataset_id": N, "dataset_version": "1.0.0-rc1", "field": "ILCEKN"}]
    assert [j["to_field"] for j in f["field_relations"]] == ["KIMLIKNO"]
    assert client.get("/api/data/v1/domain", params={"id": N, "name": "NOT_A_DOMAIN"}).json()["detail"]["code"] == "domain_not_found"


# ----------------------------------------------------------------------------- erişim sözleşmesi

def test_descriptor_answers_version_location_schema_crs_quality_access(incoming: Path, data_root: Path, tmp_path: Path, monkeypatch) -> None:
    _register(incoming, data_root, "TT01_v1", passport=PASSPORT)
    client = _client(tmp_path, monkeypatch, data_root)
    listed = {d["dataset_id"]: d for d in client.get("/api/data/v1/datasets").json()}
    assert listed[N]["declared_role"] == "administrative_neighborhood_and_special_area_boundaries" and listed[N]["latest_version"] == "1.0.0-rc1"
    d = client.get("/api/data/v1/descriptor", params={"id": N}).json()
    assert d["contract"] == "thermalis.data.contract/1" and d["version_id"] == f"{N}@1.0.0-rc1"
    assert d["asset"]["format"] == "filegdb_vector" and d["asset"]["relative_path"].endswith("Admin.gdb") and d["asset"]["layer"] == "Neighborhoods"
    assert d["asset"]["reader"] == {"driver": "OpenFileGDB", "open_mode": "read_only"}
    assert d["crs"]["name"] == "ITRF_96_UTM_Zone_35N" and d["crs"]["epsg_assumed"] is False and "PROJCS" in d["crs"]["wkt"]
    assert {f["name"] for f in d["schema"]} >= {"ILCEKN", "YAKA", "KIMLIKNO"}
    assert d["quality"]["status"] == "QA_CONDITIONAL" and d["access"]["classification"] == "RESTRICTED"
    assert d["temporal"]["currency_year"] == "2026" and d["temporal"]["valid_from"] is None
    assert {r["dataset_id"] for r in d["related_datasets"]} == {D, f"{DS}/AUX_BOUNDARY"}
    assert "uygunluk" in d["usage_note"]
    qa = client.get(d["links"]["qa"]).json()
    special = qa["special_records"]
    assert [(s["objectid"], s["classification"]) for s in special] == [(3, "DOCUMENTED_SPECIAL_RECORD")]
    assert "globalid" not in special[0]  # sentetik GDB'de GLOBALID alanı yok: uydurulmaz
    assert client.get("/api/data/v1/descriptor", params={"id": f"{DS}/NOPE"}).status_code == 404
    assert client.get("/api/data/v1/descriptor", params={"id": N, "version": "9.9.9"}).json()["detail"]["code"] == "version_not_found"


def test_restricted_physical_paths_require_local_component_token(incoming: Path, data_root: Path, tmp_path: Path, monkeypatch) -> None:
    _register(incoming, data_root, "TT01_v1", passport=PASSPORT)
    client = _client(tmp_path, monkeypatch, data_root)
    anon = client.get("/api/data/v1/access", params={"id": N}).json()
    assert anon["absolute_path"] is None and anon["conditions"]["physical_access"] == "local_component_token_required"
    assert anon["relative_path"].endswith("Admin.gdb") and anon["conditions"]["redistribution"] == "not_permitted"
    assert client.get("/api/data/v1/descriptor", params={"id": N}).json()["provenance"]["ingested_from"] is None
    bad = client.get("/api/data/v1/access", params={"id": N}, headers={"X-Thermalis-Component-Token": "guess"}).json()
    assert bad["absolute_path"] is None
    from thermalis.settings import component_token

    token = component_token()
    assert (tmp_path / "config" / "component_token").is_file()  # yapılandırma klasöründe; veri kökünde/repoda değil
    ok = client.get("/api/data/v1/access", params={"id": N}, headers={"X-Thermalis-Component-Token": token}).json()
    assert Path(ok["absolute_path"]).is_dir() and ok["read_only"] is True
    # Süreç içi yetkili bileşen aynı sonucu alır.
    assert contract.access(data_root, N, authorized=True)["absolute_path"] == ok["absolute_path"]


def test_public_dataset_exposes_location_without_token(incoming: Path, data_root: Path, tmp_path: Path, monkeypatch) -> None:
    _register(incoming, data_root, "public", passport=PASSPORT,
              release_overrides={"not_released_as_public_open_data": None, "access_classification": "PUBLIC"})
    a = contract.access(data_root, N)
    assert a["conditions"]["classification"] == "PUBLIC" and Path(a["absolute_path"]).is_dir()


def test_foreign_host_header_is_rejected(tmp_path: Path, monkeypatch, data_root: Path) -> None:
    client = _client(tmp_path, monkeypatch, data_root)
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health", headers={"Host": "attacker.example"}).status_code == 400
    monkeypatch.delenv("THERMALIS_ALLOWED_HOSTS")
    from thermalis.app import create_app

    strict = TestClient(create_app(watch=False), base_url="http://127.0.0.1:8765")
    assert strict.get("/api/health").status_code == 200
    assert TestClient(create_app(watch=False)).get("/api/health").status_code == 400  # 'testserver' üretimde kabul edilmez


# ----------------------------------------------------------------------------- tamlık ve entegrasyon uyumu

def test_completeness_groups_and_conflict_is_not_missing(incoming: Path, data_root: Path) -> None:
    _register(incoming, data_root, "TT01_v1", passport=PASSPORT)
    items = {c["item"]: c for c in contract.descriptor(data_root, N)["metadata_completeness"]}
    assert {c["group"] for c in items.values()} == {"technical", "provenance_access", "semantic", "scientific_use"}
    assert items["crs_consistency"]["state"] == "present" and items["feature_counts"]["state"] == "present"
    assert items["semantic_definitions"]["state"] == "partial"  # 'bilinmiyor' alanlar eksiktir, hata değildir
    final = catalog.version_dir(data_root, DS, "1.0.0-rc1")
    record = json.loads((final / "version.json").read_text(encoding="utf-8"))
    record["layers"]["Districts"]["crs_matches_package_wkt"] = False
    from thermalis.data.catalog_index import metadata_completeness

    states = {i: s for i, s, _ in metadata_completeness(record, json.loads((final / "quality.json").read_text(encoding="utf-8")))}
    assert states["crs_consistency"] == "conflict"  # çelişki eksiklikten ayrı bir durumdur


def test_ingest_result_carries_semantic_completeness_and_records_stay_immutable(incoming: Path, data_root: Path) -> None:
    write_ready_marker(build_package(incoming / "TT01_v1", passport=PASSPORT))
    (result,) = run_ingest(incoming, data_root, settle_seconds=0).results
    sem = next(c for c in result.completeness if c["item"] == "semantic_definitions")
    assert sem["state"] == "partial" and sem["group"] == "semantic"
    final = catalog.version_dir(data_root, DS, "1.0.0-rc1")
    before = {p.name: sha256_file(p) for p in final.iterdir() if p.is_file()}
    contract.quality(data_root, N)  # GDAL ile salt okunur kayıt okuması
    rebuild_index(data_root)
    assert {p.name: sha256_file(p) for p in final.iterdir() if p.is_file()} == before
    assert not list((final / "payload").rglob("*.lock"))
