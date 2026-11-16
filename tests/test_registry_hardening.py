"""Katman bazlı bağımsız sürümleme, içerik kimliği, metadata kaynak önceliği/kodlama ve doğrulama güvenliği.

Tüm veriler sentetiktir ve geçici klasörlerde üretilir; gerçek HD01 kaydına dokunulmaz.
"""

from __future__ import annotations

import json
import threading
import tracemalloc
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import _build_gdb, build_package, write_ready_marker
from thermalis.data import catalog, contract, semantics
from thermalis.data.catalog_index import rebuild_index
from thermalis.data.ingest import run_ingest
from thermalis.data.integrity import sha256_file
from thermalis.data.vector_inspect import CONTENT_ID_ALGORITHM, layer_facts
from thermalis.settings import Settings, component_token, save_settings

DS = "THERMALIS_DATA/TT01_ADMIN_BOUNDARIES/ISTANBUL"
N = f"{DS}/NEIGHBORHOODS"
D = f"{DS}/DISTRICTS"
PASSPORT = {"contents": {"feature_classes": [
    {"layer_name": "Neighborhoods", "field_semantics": {"ILCEKN": "Foreign key referencing Districts.KIMLIKNO"}},
]}}


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


def _two_packages(incoming: Path, data_root: Path) -> dict[str, str]:
    """v1 (1.0.0-rc1) ve yalnız Neighborhoods şeması değişen v2 (1.0.0). v1 kayıt dosyalarının özetleri döner."""
    _register(incoming, data_root, "TT01_v1", passport=PASSPORT)
    v1 = catalog.version_dir(data_root, DS, "1.0.0-rc1")
    before = {p.name: sha256_file(p) for p in v1.iterdir() if p.is_file()}
    _register(incoming, data_root, "TT01_v2", passport=PASSPORT, version="1.0.0", extra_field="AREA_HA")
    return before


# ----------------------------------------------------------------------------- katman bazlı sürümleme

def test_only_changed_layer_gets_a_new_dataset_version(incoming: Path, data_root: Path) -> None:
    before = _two_packages(incoming, data_root)
    assert [v["dataset_version"] for v in contract.list_versions(data_root, N)] == ["1.0.0-rc1", "1.0.0"]
    assert [v["dataset_version"] for v in contract.list_versions(data_root, D)] == ["1.0.0-rc1"]
    # Districts yeni pakette de var ama sürümü değişmedi; yeni paketle ilişkisi korunur.
    d = contract.descriptor(data_root, D)
    assert d["dataset_version"] == "1.0.0-rc1" and d["version_id"] == f"{D}@1.0.0-rc1"
    assert {(p["package_version"], p["change"]) for p in d["present_in_packages"]} == {("1.0.0-rc1", "new"), ("1.0.0", "unchanged")}
    n = contract.descriptor(data_root, N)
    assert (n["dataset_version"], n["package"]["package_version"]) == ("1.0.0", "1.0.0")
    assert n["content"]["algorithm"] == CONTENT_ID_ALGORITHM and n["content"]["content_id"] != d["content"]["content_id"]
    assert d["package"]["package_version"] == "1.0.0-rc1"  # dataset sürümünün doğduğu paket
    # Önceki değişmez kayıt dosyaları bayt bayt aynı kalır.
    v1 = catalog.version_dir(data_root, DS, "1.0.0-rc1")
    assert {p.name: sha256_file(p) for p in v1.iterdir() if p.is_file()} == before


def test_package_version_still_resolves_for_old_callers(incoming: Path, data_root: Path) -> None:
    _two_packages(incoming, data_root)
    via_package = contract.descriptor(data_root, D, "1.0.0")  # eski API: paket sürümüyle sorgu
    assert via_package["dataset_version"] == "1.0.0-rc1"
    assert via_package["resolved_from"] == {"package_version": "1.0.0", "change": "unchanged"}
    assert contract.descriptor(data_root, D, "1.0.0-rc1")["resolved_from"] is None
    diff = contract.diff(data_root, D, "1.0.0-rc1", "1.0.0")
    assert diff["unchanged"] is True and diff["same_dataset_version"] is True and diff["content_identical"] is True
    changed = contract.diff(data_root, N, "1.0.0-rc1", "1.0.0")
    assert changed["same_dataset_version"] is False and changed["content_identical"] is False
    with pytest.raises(contract.NotFound):
        contract.descriptor(data_root, D, "9.9.9")


def test_index_is_rebuildable_and_versions_are_deterministic(incoming: Path, data_root: Path) -> None:
    _two_packages(incoming, data_root)
    first = [(v["dataset_version"], v["content_id"]) for v in contract.list_versions(data_root, N)]
    (catalog.data_dir(data_root) / "catalog_index.sqlite").unlink()
    rebuild_index(data_root)
    assert [(v["dataset_version"], v["content_id"]) for v in contract.list_versions(data_root, N)] == first


# ----------------------------------------------------------------------------- içerik kimliği

def _gdb_with_rows(path: Path, rows: list[tuple[int, str]]) -> None:
    from osgeo import gdal, ogr, osr

    gdal.UseExceptions()
    ds = gdal.GetDriverByName("OpenFileGDB").Create(str(path), 0, 0, 0, gdal.GDT_Unknown)
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(32635)
    layer = ds.CreateLayer("L", srs, ogr.wkbPoint)
    layer.CreateField(ogr.FieldDefn("CODE", ogr.OFTInteger))
    name = ogr.FieldDefn("AD", ogr.OFTString)
    name.SetWidth(40)
    layer.CreateField(name)
    for code, label in rows:
        f = ogr.Feature(layer.GetLayerDefn())
        f.SetField("CODE", code)
        f.SetField("AD", label)
        f.SetGeometry(ogr.CreateGeometryFromWkt(f"POINT ({500000 + code} 4500000)"))
        layer.CreateFeature(f)
    ds = None


def test_content_id_is_order_and_fid_independent_but_value_sensitive(tmp_path: Path) -> None:
    rows = [(1, "KADIKÖY"), (2, "FİKİRTEPE"), (3, "ADALAR")]
    _gdb_with_rows(tmp_path / "a.gdb", rows)
    _gdb_with_rows(tmp_path / "b.gdb", list(reversed(rows)))  # aynı içerik, farklı FID sırası
    _gdb_with_rows(tmp_path / "c.gdb", [(1, "KADIKÖY"), (2, "FIKIRTEPE"), (3, "ADALAR")])  # tek karakter farkı
    a, b, c = (layer_facts(tmp_path / f"{n}.gdb", "L") for n in "abc")
    assert a["algorithm"] == CONTENT_ID_ALGORITHM and a["feature_count"] == 3
    assert a["content_id"] == b["content_id"]
    assert a["content_id"] != c["content_id"] and a["schema_sha256"] == c["schema_sha256"]


def test_content_id_includes_schema_and_domains(tmp_path: Path) -> None:
    _build_gdb(tmp_path / "x.gdb", None)
    _build_gdb(tmp_path / "y.gdb", "AREA_HA")
    x, y = layer_facts(tmp_path / "x.gdb", "Neighborhoods"), layer_facts(tmp_path / "y.gdb", "Neighborhoods")
    assert x["schema_sha256"] != y["schema_sha256"] and x["content_id"] != y["content_id"]
    assert layer_facts(tmp_path / "x.gdb", "Districts")["content_id"] == layer_facts(tmp_path / "y.gdb", "Districts")["content_id"]


def test_content_id_streams_without_loading_all_features(tmp_path: Path) -> None:
    rows = [(i, f"KAYIT {i:06d} " + "x" * 30) for i in range(20000)]
    _gdb_with_rows(tmp_path / "big.gdb", rows)
    tracemalloc.start()
    facts = layer_facts(tmp_path / "big.gdb", "L")
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert facts["feature_count"] == 20000
    # 20 000 kaydın tamamı bellekte tutulsaydı Python tarafı birkaç MB'ı aşardı; akış ~sabit bellekle çalışır.
    assert peak < 2 * 1024 * 1024, peak


# ----------------------------------------------------------------------------- kaynak önceliği ve kodlama

def test_corrupted_secondary_labels_and_meanings_are_conflicts_not_accepted(incoming: Path, data_root: Path) -> None:
    corrupted = {"contents": {"feature_classes": [{
        "layer_name": "Neighborhoods",
        "field_semantics": {"ILCEKN": "Ä°lÃ§e kodu", "AD": "Name of neighborhood"},
        "field_domains": {"ILCEKN": {"domain": "Ilce_UAVT", "values": {"1421": "KADIKÃ–Y", "1103": "ADALAR", "9999": "YOK"}}},
    }]}}
    _register(incoming, data_root, "bad_text", passport=corrupted)
    sem = contract.semantic_dictionary(data_root, N)
    fields = {f["field"]: f for f in sem["fields"]}
    assert (fields["ILCEKN"]["meaning"], fields["ILCEKN"]["status"]) == (None, semantics.UNKNOWN)  # bozuk metin kabul edilmez
    assert fields["AD"]["status"] == semantics.SOURCE_DECLARED  # temiz metin etkilenmez
    issues = {(c["kind"], c["item"], c["issue"]) for c in sem["conflicts"]}
    assert ("field_meaning", "ILCEKN", "source_mojibake_sequence") in issues
    codes = contract.domain_codes(data_root, N, None, "Ilce_UAVT")
    labels = {c["code"]: c["label"] for c in codes["codes"]}
    assert labels == {"1103": "ADALAR", "1421": "KADIKÖY"}  # yetkili kaynak değişmedi, tahmin yok
    conflict = {(c["item"], c["issue"]) for c in codes["conflicts"]}
    assert ("1421", "secondary_mojibake_sequence") in conflict and ("9999", "code_not_in_authoritative_source") in conflict
    assert codes["source_priority"][0] == "gdb_field_domain"


def test_editor_tracking_fields_are_inferred_and_structural_fields_are_not_missing(tmp_path: Path) -> None:
    record = {"documents": [], "data_dictionary": [
        {"dataset": "L", "field": "OBJECTID", "type": "OID"}, {"dataset": "L", "field": "Shape", "type": "Geometry"}],
        "layers": {"L": {"fields": [{"name": "CREATED_USER", "type": "String"}, {"name": "LAST_EDITED_DATE", "type": "DateTime"},
                                    {"name": "CREATED_DATE", "type": "Integer"}]}}}
    entries = {e["field"]: e for e in semantics.source_entries(tmp_path, record, {})}
    for name in ("CREATED_USER", "LAST_EDITED_DATE"):
        assert entries[name]["status"] == semantics.INFERRED_UNVERIFIED and entries[name]["origin"] == "name_convention:esri_editor_tracking"
    assert entries["CREATED_DATE"]["status"] == semantics.UNKNOWN  # tip uyuşmuyor: çıkarım yapılmaz
    assert {entries[n]["field_role"] for n in ("OBJECTID", "Shape")} == {"structural"}
    assert all(e["status"] != semantics.USER_VERIFIED for e in entries.values())


# ----------------------------------------------------------------------------- doğrulama güvenliği

def test_verification_requires_an_authorized_local_channel(incoming: Path, data_root: Path, tmp_path: Path, monkeypatch) -> None:
    from thermalis.app import UI_COOKIE, issue_ui_code

    _register(incoming, data_root, "TT01_v1", passport=PASSPORT)
    client = _client(tmp_path, monkeypatch, data_root)
    body = {"dataset_id": N, "version": "1.0.0-rc1", "field": "YAKA", "meaning": "Side code", "verified_by": "someone"}
    assert client.post("/api/data/v1/semantics/verify", json=body).json()["detail"]["code"] == "verification_not_authorized"
    bad = client.post("/api/data/v1/semantics/verify", json=body, headers={"X-Thermalis-Component-Token": "guess"})
    assert bad.status_code == 403
    assert client.get("/api/session").json() == {"verification_allowed": False, "channel": None, "identity_verified": False}
    assert client.post("/api/session/claim", json={"code": "not-a-code"}).status_code == 403
    code = issue_ui_code()
    claimed = client.post("/api/session/claim", json={"code": code})
    assert claimed.status_code == 200 and UI_COOKIE in claimed.cookies
    assert client.post("/api/session/claim", json={"code": code}).status_code == 403  # tek kullanımlık
    assert client.get("/api/session").json()["channel"] == "ui_session"
    ok = client.post("/api/data/v1/semantics/verify", json=body)
    assert ok.status_code == 200 and ok.json()["status"] == semantics.USER_VERIFIED
    assert (ok.json()["verified_by"], ok.json()["verification_auth"]) == ("someone", "ui_session")
    (entry,) = semantics.read_log(data_root)[0]
    assert entry["identity_verified"] is False and entry["declared_by"] == "someone"  # beyan, kimlik doğrulaması değil
    assert entry["previous"]["status"] == semantics.UNKNOWN and entry["content_id"]


def test_verification_cannot_bind_to_a_package_version_alias(incoming: Path, data_root: Path, tmp_path: Path, monkeypatch) -> None:
    _two_packages(incoming, data_root)
    client = _client(tmp_path, monkeypatch, data_root)
    auth = {"X-Thermalis-Component-Token": component_token()}
    body = {"dataset_id": D, "version": "1.0.0", "field": "AD", "meaning": "District name"}
    r = client.post("/api/data/v1/semantics/verify", json=body, headers=auth)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "verification_version_mismatch"
    assert not semantics.read_log(data_root)[0]


def test_verification_for_other_content_is_not_applied(incoming: Path, data_root: Path) -> None:
    _register(incoming, data_root, "TT01_v1", passport=PASSPORT)
    semantics.append_verification(data_root, {"dataset_id": N, "version": "1.0.0-rc1", "field": "YAKA", "meaning": "Side code"},
                                  content_id="lc1:not-this-content")
    rebuild_index(data_root)
    assert contract.field_info(data_root, N, None, "YAKA")["status"] == semantics.UNKNOWN


def test_tampered_log_is_detected_and_blocks_further_writes(incoming: Path, data_root: Path, tmp_path: Path, monkeypatch) -> None:
    _register(incoming, data_root, "TT01_v1", passport=PASSPORT)
    cid = contract.field_info(data_root, N, None, "YAKA")["content_id"]
    for meaning in ("Side code", "Bosphorus side"):
        semantics.append_verification(data_root, {"dataset_id": N, "version": "1.0.0-rc1", "field": "YAKA", "meaning": meaning}, content_id=cid)
    path = semantics.verifications_path(data_root)
    lines = path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["values"]["meaning"] = "Forged meaning"
    path.write_text(lines[0] + "\n" + json.dumps(tampered, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    rebuild_index(data_root)
    log = contract.semantic_dictionary(data_root, N)["verification_log"]
    assert (log["status"], log["broken_at_line"], log["reason"]) == ("broken", 2, "hash_chain_mismatch")
    assert contract.field_info(data_root, N, None, "YAKA")["meaning"] == "Side code"  # sahte satır uygulanmaz
    with pytest.raises(ValueError):
        semantics.append_verification(data_root, {"dataset_id": N, "version": "1.0.0-rc1", "field": "YAKA", "meaning": "x"}, content_id=cid)
    client = _client(tmp_path, monkeypatch, data_root)
    r = client.post("/api/data/v1/semantics/verify", json={"dataset_id": N, "version": "1.0.0-rc1", "field": "YAKA", "meaning": "y"},
                    headers={"X-Thermalis-Component-Token": component_token()})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "verification_log_broken"


def test_concurrent_verifications_keep_the_chain_valid(data_root: Path) -> None:
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(5):
                semantics.append_verification(data_root, {"dataset_id": N, "version": "1.0.0-rc1", "field": f"F{n}", "meaning": f"m{i}"},
                                              content_id="lc1:x")
        except BaseException as exc:  # noqa: BLE001 - testte tüm hatalar raporlanır
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    valid, integrity = semantics.read_log(data_root)
    assert integrity["status"] == "ok" and len(valid) == 40
    assert [e["seq"] for e in valid] == list(range(1, 41))
