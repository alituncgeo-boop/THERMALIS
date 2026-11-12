"""Otomatik klasör izleme, işlem durumları, katalog görünümleri ve arayüz API'leri (uçtan uca, sentetik paketlerle)."""

from __future__ import annotations

import csv
import io
import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import build_package, write_manifest, write_ready_marker
from thermalis.data import catalog
from thermalis.data.ingest import IngestLock, forget_failure, load_intake
from thermalis.data.integrity import sha256_file
from thermalis.data.intake import IntakeWatcher
from thermalis.settings import Settings

DATASET = "THERMALIS_DATA/TT01_ADMIN_BOUNDARIES/ISTANBUL"
DISTRICTS = f"{DATASET}/DISTRICTS"
NEIGHBORHOODS = f"{DATASET}/NEIGHBORHOODS"
PASSPORT = {
    "identity": {"dataset_title": "Test Administrative Boundaries", "theme": ["administrative_boundaries"], "geographic_scope": "Istanbul, Turkey"},
    "provenance": {"providing_organization": "Test Kurumu CBS", "origin_system": "TEST"},
    "spatial_temporal": {"temporal_currency_year": 2026, "precise_valid_from": None, "precise_valid_to": None},
}
LAYER_META = {
    "Districts": {"title": "Districts", "summary": "İlçe sınırları", "description": "<DIV><P><SPAN>İlçe <b>sınırları</b> verisi</SPAN></P></DIV>",
                  "credits": "Test Kurumu", "tags": "ilçe", "accessConstraints": "<P>İzinsiz kullanılamaz.</P>"},
    "Neighborhoods": {"title": "Neighborhoods", "summary": "Mahalle sınırları", "description": "<P>Mahalle verisi</P>",
                      "credits": "Test Kurumu", "tags": "mahalle", "accessConstraints": ""},
}


@pytest.fixture()
def incoming(tmp_path: Path) -> Path:
    folder = tmp_path / "INCOMING"
    folder.mkdir()
    return folder


def _watcher(incoming: Path, data_root: Path, settle: int = 0) -> IntakeWatcher:
    settings = Settings(data_root=str(data_root), transfer_incoming_dir=str(incoming), transfer_settle_seconds=settle)
    return IntakeWatcher(lambda: settings)


def _state(data_root: Path, name: str) -> dict:
    return next(e for e in load_intake(data_root)["packages"].values() if e["name"] == name)


def _tree(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in root.rglob("*") if p.is_file()}


def _client(tmp_path: Path, monkeypatch, data_root: Path, *, incoming: Path | None = None) -> TestClient:
    from thermalis.app import create_app
    from thermalis.settings import save_settings

    monkeypatch.setenv("THERMALIS_CONFIG_DIR", str(tmp_path / "config"))
    save_settings(Settings(data_root=str(data_root), transfer_incoming_dir=str(incoming) if incoming else None, transfer_settle_seconds=0))
    return TestClient(create_app(watch=False))


def _wait(predicate, timeout: float = 60.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError("koşul zaman aşımında sağlanmadı")


# ----------------------------------------------------------------------------- izleyici

def test_watcher_waits_for_stable_package_then_registers(incoming: Path, data_root: Path) -> None:
    build_package(incoming / "TT01_v1")
    w = _watcher(incoming, data_root)
    w.scan_once()
    first = _state(data_root, "TT01_v1")
    assert first["state"] == "WAITING_FOR_COMPLETION" and first["reason_code"] == "STABILITY_CHECK"
    assert catalog.list_versions(data_root) == []
    w.scan_once()
    assert _state(data_root, "TT01_v1")["state"] == "REGISTERED"
    assert [v["version"] for v in catalog.list_versions(data_root)] == ["1.0.0-rc1"]
    assert w.status.last_result == "ok" and w.status.processed == 1
    history = [h["state"] for h in _state(data_root, "TT01_v1")["history"]]
    assert history == ["WAITING_FOR_COMPLETION", "READY", "VALIDATING", "INGESTING", "REGISTERED"]


def test_package_changing_between_scans_is_not_processed(incoming: Path, data_root: Path) -> None:
    package = build_package(incoming / "TT01_v1")
    w = _watcher(incoming, data_root)
    w.scan_once()
    (package / "PACKAGE" / "README.md").write_text("# kopyalama sürüyor\n", encoding="utf-8")
    write_manifest(package)  # içerik değişti (kopyalama sürüyor gibi)
    w.scan_once()
    assert _state(data_root, "TT01_v1")["state"] == "WAITING_FOR_COMPLETION"
    w.scan_once()
    assert _state(data_root, "TT01_v1")["state"] == "REGISTERED"


def test_ready_marker_allows_immediate_processing_and_mismatch_fails(incoming: Path, data_root: Path) -> None:
    good = build_package(incoming / "TT01_ready")
    write_ready_marker(good)
    bad = build_package(incoming / "TT01_bad_marker", version="1.0.1")
    write_ready_marker(bad, sha="0" * 64)
    w = _watcher(incoming, data_root, settle=3600)  # bekleme süresi uzun: yalnız işaretli paket geçebilir
    w.scan_once()
    assert _state(data_root, "TT01_ready")["state"] == "REGISTERED"
    bad_state = _state(data_root, "TT01_bad_marker")
    assert bad_state["state"] == "FAILED" and "ready_marker_manifest_mismatch" in bad_state["reason"]
    final = catalog.version_dir(data_root, DATASET, "1.0.0-rc1")
    assert (final / "payload" / "THERMALIS_READY.json").is_file()  # işaret kopyalanır, bütünlük denetimi onu yok sayar


def test_missing_manifest_is_never_processed(incoming: Path, data_root: Path) -> None:
    package = build_package(incoming / "no_manifest")
    (package / "PACKAGE" / "MANIFEST.json").unlink()
    w = _watcher(incoming, data_root)
    for _ in range(3):
        w.scan_once()
    assert _state(data_root, "no_manifest")["state"] == "WAITING_FOR_COMPLETION"
    assert catalog.list_versions(data_root) == []


def test_corrupt_sha256_fails_once_and_is_retried_only_on_request(incoming: Path, data_root: Path) -> None:
    package = build_package(incoming / "corrupt")
    readme = package / "PACKAGE" / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8").replace("Test", "Tset"), encoding="utf-8")  # aynı boyut, farklı içerik
    w = _watcher(incoming, data_root)
    w.scan_once()
    w.scan_once()
    entry = _state(data_root, "corrupt")
    assert entry["state"] == "FAILED" and "INT-01" in entry["reason"] and entry["reason_code"] == "check_failed"
    runs = list((catalog.data_dir(data_root) / "_ingest" / "runs").iterdir())
    w.scan_once()  # değişmemiş başarısız paket: yeniden işlenmez
    assert list((catalog.data_dir(data_root) / "_ingest" / "runs").iterdir()) == runs
    assert catalog.list_versions(data_root) == []
    assert forget_failure(data_root, entry["path"]) is True
    w.scan_once()
    w.scan_once()
    assert len(list((catalog.data_dir(data_root) / "_ingest" / "runs").iterdir())) == len(runs) + 1


def test_unsupported_format_is_reported_as_adapter_required(incoming: Path, data_root: Path) -> None:
    package = build_package(incoming / "landsat_like")
    (package / "LC09").mkdir()
    (package / "LC09" / "LC09_L2SP_180032_20240801_SR_B4.TIF").write_bytes(b"II*\x00")
    (package / "LC09" / "LC09_L2SP_180032_20240801_MTL.json").write_text("{}", encoding="utf-8")
    write_manifest(package)
    write_ready_marker(package)
    _watcher(incoming, data_root).scan_once()
    entry = _state(data_root, "landsat_like")
    assert entry["state"] == "ADAPTER_REQUIRED" and entry["reason_code"] == "format_not_supported"
    gap = json.loads(Path(entry["support_gap_path"]).read_text(encoding="utf-8"))
    assert set(gap["unsupported_by_suffix"]) == {".tif", ".json"}
    assert catalog.list_versions(data_root) == []


def test_multiple_packages_in_order_and_prior_version_preserved(incoming: Path, data_root: Path) -> None:
    write_ready_marker(build_package(incoming / "a_TT01_v1"))
    write_ready_marker(build_package(incoming / "b_TT01_v2", version="1.0.0"))
    _watcher(incoming, data_root).scan_once()
    assert _state(data_root, "a_TT01_v1")["state"] == "REGISTERED" and _state(data_root, "b_TT01_v2")["state"] == "REGISTERED"
    assert sorted(v["version"] for v in catalog.list_versions(data_root)) == ["1.0.0", "1.0.0-rc1"]
    v1 = catalog.version_dir(data_root, DATASET, "1.0.0-rc1")
    before = _tree(v1)
    _watcher(incoming, data_root).scan_once()  # tekrar: aynı paketler yeniden kaydedilmez
    assert _tree(v1) == before
    assert {_state(data_root, n)["state"] for n in ("a_TT01_v1", "b_TT01_v2")} == {"REGISTERED"}


def test_state_survives_restart_and_interrupted_work_is_recovered(incoming: Path, data_root: Path) -> None:
    write_ready_marker(build_package(incoming / "TT01_v1"))
    # Önceki çalıştırma kayıt sırasında çökmüş gibi: durum INGESTING, yarım staging klasörü var.
    path = catalog.data_dir(data_root) / "_ingest" / "intake.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": "1.0", "packages": {str(incoming / "TT01_v1"): {
        "name": "TT01_v1", "path": str(incoming / "TT01_v1"), "state": "INGESTING", "reason": "", "history": []}}}), encoding="utf-8")
    (catalog.data_dir(data_root) / "_staging" / "ing-crashed").mkdir(parents=True)
    w = _watcher(incoming, data_root)  # "yeniden başlatılmış" uygulama
    w.scan_once()
    entry = _state(data_root, "TT01_v1")
    assert entry["state"] == "REGISTERED"
    assert entry["history"][0]["state"] == "DETECTED" and "yarıda kaldı (INGESTING)" in entry["history"][0]["reason"]
    assert not any((catalog.data_dir(data_root) / "_staging").iterdir())
    assert any((catalog.data_dir(data_root) / "_failed").iterdir())  # yarım staging karantinaya alındı, silinmedi
    restarted = _watcher(incoming, data_root)
    restarted.scan_once()
    assert _state(data_root, "TT01_v1")["state"] == "REGISTERED"


def test_watcher_reports_busy_and_missing_folder(incoming: Path, data_root: Path, tmp_path: Path) -> None:
    w = _watcher(incoming, data_root)
    with IngestLock(data_root):
        w.scan_once()
    assert w.status.last_result == "busy"
    gone = _watcher(tmp_path / "does_not_exist", data_root)
    gone.scan_once()
    assert gone.status.last_result == "incoming_missing"
    off = IntakeWatcher(lambda: Settings(data_root=str(data_root)))
    off.scan_once()
    assert off.status.last_result == "not_configured" and off.status.enabled is False


def test_app_startup_scans_folder_and_detects_new_packages(incoming: Path, data_root: Path, tmp_path: Path, monkeypatch) -> None:
    from thermalis.app import create_app
    from thermalis.settings import save_settings

    monkeypatch.setenv("THERMALIS_CONFIG_DIR", str(tmp_path / "config"))
    save_settings(Settings(data_root=str(data_root), transfer_incoming_dir=str(incoming), transfer_settle_seconds=0,
                           transfer_scan_interval_seconds=10))
    write_ready_marker(build_package(incoming / "left_while_closed"))  # uygulama kapalıyken bırakıldı
    with TestClient(create_app(watch=True)) as client:
        _wait(lambda: any(p["state"] == "REGISTERED" for p in client.get("/api/data/intake").json()["packages"]))
        write_ready_marker(build_package(incoming / "dropped_while_running", version="1.0.0"))
        client.post("/api/data/intake/scan")
        _wait(lambda: sum(p["state"] == "REGISTERED" for p in client.get("/api/data/intake").json()["packages"]) == 2)
        status = client.get("/api/data/intake").json()
        assert status["watcher"]["enabled"] is True and status["counts"]["REGISTERED"] == 2
        assert {d["version"] for d in client.get("/api/data/catalog").json()["datasets"]} == {"1.0.0"}  # katalogda son sürüm


def test_every_intake_state_and_reason_code_is_translated() -> None:
    from thermalis.data.ingest import CANDIDATE_TO_INTAKE, INTAKE_STATES, REASON_CODES

    locales = Path(__file__).resolve().parents[1] / "frontend" / "src" / "i18n" / "locales"
    for lang in ("tr", "en"):
        intake = json.loads((locales / f"{lang}.json").read_text(encoding="utf-8"))["data"]["intake"]
        assert set(INTAKE_STATES) <= set(intake["state"]), lang
        assert set(REASON_CODES) <= set(intake["reason"]), lang
    assert set(CANDIDATE_TO_INTAKE) - {"READY"} <= set(REASON_CODES)  # READY nedensiz işlenir


# ----------------------------------------------------------------------------- katalog görünümleri ve API

def test_catalog_lists_layers_as_separate_datasets_with_real_metadata(incoming: Path, data_root: Path, tmp_path: Path, monkeypatch) -> None:
    write_ready_marker(build_package(incoming / "TT01_v1", passport=PASSPORT, layer_metadata=LAYER_META))
    _watcher(incoming, data_root).scan_once()
    client = _client(tmp_path, monkeypatch, data_root)
    items = {d["id"]: d for d in client.get("/api/data/catalog").json()["datasets"]}
    assert set(items) == {DISTRICTS, NEIGHBORHOODS}  # yardımcı katman listelenmez
    d, n = items[DISTRICTS], items[NEIGHBORHOODS]
    assert d["asset_id"] == n["asset_id"] and d["package_id"] == n["package_id"] == "TT01_ADMIN_BOUNDARIES_v1.0.0-rc1"
    assert (d["feature_count"], n["feature_count"]) == (2, 3)
    assert d["package_version"] == "1.0.0-rc1" and d["format_key"] == "filegdb_vector"
    assert d["category"] == "administrative_boundaries" and d["place"] == "Istanbul"
    assert d["source_institution"] == "Test Kurumu" and d["description"] == "İlçe sınırları verisi"  # HTML yorumlanmadan metne
    assert d["temporal"] == {"state": "partial", "currency_year": "2026", "valid_from": None, "valid_to": None}
    assert d["quality_status"] == "QA_CONDITIONAL" and d["access_class"] == "RESTRICTED"
    assert d["integrity"]["status"] == "PASS" and d["integrity"]["last_successful_at"] == d["registered_at"]
    assert d["spatial_extent"]["crs"] == "ITRF_96_UTM_Zone_35N"

    meta = client.get("/api/data/dataset", params={"id": NEIGHBORHOODS}).json()
    assert meta["identity"]["version"] == "1.0.0-rc1" and [v["version"] for v in meta["version_history"]] == ["1.0.0-rc1"]
    assert {f["name"] for f in meta["fields"]} >= {"ILCEKN", "YAKA", "KIMLIKNO", "AD"}
    assert next(f for f in meta["fields"] if f["name"] == "KIMLIKNO")["null_count"] == 1
    assert {d_["name"]: len(d_["values"]) for d_ in meta["domains"]} == {"Ilce_UAVT": 2, "YAKA": 2}
    assert {f["category"] for f in meta["quality"]["findings"]} == {"NEIGHBORHOOD_OUTSIDE_PARENT", "INTENTIONAL_NULL_IDENTIFIER"}
    assert meta["quality"]["gates"][0]["gate_id"] == "G02"
    assert {r["layer_name"]: r["shares_asset"] for r in meta["related_datasets"]} == {"Districts": True, "Aux_Boundary": True}
    assert meta["access"]["classification"] == "RESTRICTED" and meta["access"]["public_release"] is False
    assert meta["crs"]["transformed"] is False and meta["record_issues"] == []
    assert any(a["kind"] == "container" for a in meta["assets"])
    assert client.get("/api/data/dataset", params={"id": f"{DATASET}/NOPE"}).status_code == 404


def test_unknown_metadata_stays_unknown(incoming: Path, data_root: Path, tmp_path: Path, monkeypatch) -> None:
    write_ready_marker(build_package(incoming / "bare"))  # pasaport ve katman açıklaması yok
    _watcher(incoming, data_root).scan_once()
    client = _client(tmp_path, monkeypatch, data_root)
    item = client.get("/api/data/catalog").json()["datasets"][0]
    assert item["category"] is None and item["place"] is None and item["description"] is None
    assert item["temporal"] == {"state": "unknown", "currency_year": None, "valid_from": None, "valid_to": None}
    assert item["source_institution"] == "Test Kurumu"  # paketin RELEASE_STATUS.data_owner beyanı
    assert item["metadata_completeness"]["missing"] >= 2  # lisans belgesi ve anlamsal tanımlar yok


def test_export_catalog_json_and_csv_are_real_and_machine_readable(incoming: Path, data_root: Path, tmp_path: Path, monkeypatch) -> None:
    write_ready_marker(build_package(incoming / "TT01_v1", passport=PASSPORT, layer_metadata=LAYER_META))
    _watcher(incoming, data_root).scan_once()
    client = _client(tmp_path, monkeypatch, data_root)
    as_json = client.get("/api/data/export", params={"format": "json"})
    assert as_json.status_code == 200 and "attachment" in as_json.headers["content-disposition"]
    assert [d["id"] for d in as_json.json()["datasets"]] == [DISTRICTS, NEIGHBORHOODS]
    rows = list(csv.DictReader(io.StringIO(client.get("/api/data/export", params={"format": "csv"}).content.decode("utf-8"))))
    assert [r["feature_count"] for r in rows] == ["2", "3"] and rows[0]["quality_status"] == "QA_CONDITIONAL"
    assert "." in rows[0]["extent_minx"] and "," not in rows[0]["extent_minx"]  # nokta ondalık
    assert rows[0]["temporal_state"] == "partial"
    assert client.get("/api/data/export", params={"format": "xml"}).status_code == 400


def test_verify_endpoint_records_reverification(incoming: Path, data_root: Path, tmp_path: Path, monkeypatch) -> None:
    write_ready_marker(build_package(incoming / "TT01_v1"))
    _watcher(incoming, data_root).scan_once()
    client = _client(tmp_path, monkeypatch, data_root)
    assert client.post("/api/data/verify", json={"dataset_id": DATASET, "version": "1.0.0-rc1"}).json()["status"] == "PASS"
    meta = client.get("/api/data/dataset", params={"id": DISTRICTS}).json()
    assert [h["kind"] for h in meta["integrity"]["history"]] == ["registration", "reverification"]
    assert client.post("/api/data/verify", json={"dataset_id": f"{DATASET}X", "version": "1.0.0"}).status_code == 404


def test_manual_add_and_upload_new_version(tmp_path: Path, data_root: Path, monkeypatch) -> None:
    elsewhere = tmp_path / "manual"
    elsewhere.mkdir()
    pkg = build_package(elsewhere / "TT01_manual")
    other = build_package(elsewhere / "OTHER", release_overrides={"dataset_id": "THERMALIS_DATA/OT01_X/ISTANBUL"})
    client = _client(tmp_path, monkeypatch, data_root)
    assert client.post("/api/data/intake/submit", json={"path": "relative/path"}).json()["detail"]["code"] == "path_not_absolute"
    assert client.post("/api/data/intake/submit", json={"path": str(tmp_path / "missing")}).json()["detail"]["code"] == "path_not_found"
    mismatch = client.post("/api/data/intake/submit", json={"path": str(other), "expect_dataset_id": DATASET})
    assert mismatch.status_code == 409 and mismatch.json()["detail"]["code"] == "package_dataset_mismatch"
    assert client.post("/api/data/intake/submit", json={"path": str(pkg)}).status_code == 202
    _wait(lambda: any(p["state"] == "REGISTERED" for p in client.get("/api/data/intake").json()["packages"]))
    entry = next(p for p in client.get("/api/data/intake").json()["packages"] if p["name"] == "TT01_manual")
    assert entry["source"] == "manual"
    newer = build_package(elsewhere / "TT01_manual_v2", version="1.0.0")
    assert client.post("/api/data/intake/submit", json={"path": str(newer), "expect_dataset_id": DATASET}).status_code == 202
    _wait(lambda: len(catalog.list_versions(data_root)) == 2)


def test_settings_manage_transfer_folder(tmp_path: Path, data_root: Path, monkeypatch, incoming: Path) -> None:
    client = _client(tmp_path, monkeypatch, data_root)
    s = client.put("/api/settings", json={"transfer_incoming_dir": str(incoming), "transfer_scan_interval_seconds": 45,
                                         "transfer_settle_seconds": 0, "transfer_watch_enabled": False}).json()
    assert (s["transfer_incoming_dir"], s["transfer_scan_interval_seconds"], s["transfer_watch_enabled"]) == (str(incoming), 45, False)
    assert client.put("/api/settings", json={"transfer_incoming_dir": "relative"}).json()["detail"]["code"] == "path_not_absolute"
    assert client.put("/api/settings", json={"transfer_incoming_dir": str(tmp_path / "nope")}).json()["detail"]["code"] == "path_not_found"
    assert client.put("/api/settings", json={"transfer_scan_interval_seconds": 1}).status_code == 422
    assert client.put("/api/settings", json={"transfer_incoming_dir": ""}).json()["transfer_incoming_dir"] is None


def test_restricted_data_is_never_sent_out(incoming: Path, data_root: Path, monkeypatch) -> None:
    """Entegrasyon yerel kalır: kayıt boyunca hiçbir ağ bağlantısı açılmaz."""
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("ağ bağlantısı denendi")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    write_ready_marker(build_package(incoming / "TT01_v1"))
    _watcher(incoming, data_root).scan_once()
    assert _state(data_root, "TT01_v1")["state"] == "REGISTERED" and _state(data_root, "TT01_v1")["access"] == "RESTRICTED"
