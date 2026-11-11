from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from thermalis.app import create_app


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("THERMALIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("thermalis.settings.default_data_root", lambda: tmp_path / "data")
    return TestClient(create_app())


def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"


def test_status_is_honest(client: TestClient) -> None:
    body = client.get("/api/status").json()
    assert set(body["modules"].values()) == {"not_started"}
    assert set(body["analysis_domains"]) == {"heat", "drought", "flood", "coast", "fire", "extremes"}


def test_default_settings(client: TestClient, tmp_path: Path) -> None:
    body = client.get("/api/settings").json()
    assert body["language"] == "tr"
    assert body["data_root_is_default"] is True
    assert body["active_project_id"] is None


def test_language_persists(client: TestClient) -> None:
    assert client.put("/api/settings", json={"language": "en"}).json()["language"] == "en"
    assert client.get("/api/settings").json()["language"] == "en"


def test_invalid_language_rejected(client: TestClient) -> None:
    assert client.put("/api/settings", json={"language": "de"}).status_code == 422


def test_data_root_must_be_absolute(client: TestClient) -> None:
    response = client.put("/api/settings", json={"data_root": "relative/path"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "data_root_not_absolute"


def test_data_root_change(client: TestClient, tmp_path: Path) -> None:
    target = tmp_path / "other root"
    body = client.put("/api/settings", json={"data_root": str(target)}).json()
    assert Path(body["data_root"]) == target
    assert body["data_root_is_default"] is False
    assert target.is_dir()


def test_create_project_and_select(client: TestClient, tmp_path: Path) -> None:
    created = client.post("/api/projects", json={"name": "  İstanbul   Pilot ", "description": "Pilot"})
    assert created.status_code == 201
    project = created.json()
    assert project["name"] == "İstanbul Pilot"
    assert project["id"].startswith("prj-istanbul-pilot-")
    assert (tmp_path / "data" / "projects" / project["id"] / "project.json").is_file()

    assert client.get("/api/settings").json()["active_project_id"] == project["id"]
    assert [p["id"] for p in client.get("/api/projects").json()] == [project["id"]]
    assert client.get(f"/api/projects/{project['id']}").json()["name"] == "İstanbul Pilot"


def test_duplicate_project_name_turkish_aware(client: TestClient) -> None:
    assert client.post("/api/projects", json={"name": "İstanbul Pilot"}).status_code == 201
    response = client.post("/api/projects", json={"name": "ISTANBUL pilot"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "project_name_exists"


def test_blank_project_name_rejected(client: TestClient) -> None:
    assert client.post("/api/projects", json={"name": "   "}).status_code == 422


def test_select_unknown_project(client: TestClient) -> None:
    response = client.put("/api/settings", json={"active_project_id": "prj-nope-000000"})
    assert response.status_code == 404


def test_project_id_path_traversal_blocked(client: TestClient) -> None:
    assert client.get("/api/projects/..%2F..%2Fsecret").status_code == 404
    assert client.put("/api/settings", json={"active_project_id": "../../x"}).status_code == 404


def test_unknown_api_route(client: TestClient) -> None:
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"


def test_ui_served_with_spa_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("THERMALIS_CONFIG_DIR", str(tmp_path / "config"))
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<html>THERMALIS</html>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("no", encoding="utf-8")
    client = TestClient(create_app(ui))
    assert "THERMALIS" in client.get("/data").text
    assert "THERMALIS" in client.get("/../secret.txt").text
