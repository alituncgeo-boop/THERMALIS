"""THERMALIS yerel uygulama sunucusu.

Yalnız ``127.0.0.1`` üzerinde çalışır. ``/api`` altında JSON servisleri,
geri kalan yollarda derlenmiş arayüz sunulur.
Hatalar arayüzün çevirebilmesi için makine okunur ``code`` ile döner.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import __version__
from .projects import (
    Project,
    ProjectCreate,
    ProjectError,
    create_project,
    get_project,
    list_projects,
)
from .settings import (
    Language,
    Settings,
    effective_data_root,
    load_settings,
    save_settings,
)

# Her bileşenin gerçek durumu. Bir bileşen yazılmadıkça "not_started" kalır;
# arayüz bu değeri dürüst bir boş durum olarak gösterir.
MODULE_STATUS: dict[str, str] = {
    "data": "not_started",
    "analysis_lab": "not_started",
    "risk_engine": "not_started",
    "city3d": "not_started",
    "intelligence": "not_started",
}
ANALYSIS_DOMAINS = ("heat", "drought", "flood", "coast", "fire", "extremes")


class SettingsView(BaseModel):
    language: Language
    data_root: str
    data_root_is_default: bool
    active_project_id: str | None


class SettingsUpdate(BaseModel):
    language: Language | None = None
    data_root: str | None = None
    active_project_id: str | None = None


def _error(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


def _settings_view(settings: Settings) -> SettingsView:
    return SettingsView(
        language=settings.language,
        data_root=str(effective_data_root(settings)),
        data_root_is_default=settings.data_root is None,
        active_project_id=settings.active_project_id,
    )


def _check_writable(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".thermalis_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise _error(400, "data_root_not_writable") from exc


def create_app(static_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="THERMALIS", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/api/status")
    def status() -> dict:
        return {
            "modules": MODULE_STATUS,
            "analysis_domains": {domain: "not_started" for domain in ANALYSIS_DOMAINS},
        }

    @app.get("/api/settings", response_model=SettingsView)
    def read_settings() -> SettingsView:
        return _settings_view(load_settings())

    @app.put("/api/settings", response_model=SettingsView)
    def update_settings(update: SettingsUpdate) -> SettingsView:
        settings = load_settings()
        if update.language is not None:
            settings.language = update.language
        if update.data_root is not None:
            candidate = Path(update.data_root.strip())
            if not candidate.is_absolute():
                raise _error(400, "data_root_not_absolute")
            _check_writable(candidate)
            settings.data_root = str(candidate)
            settings.active_project_id = None  # proje başka bir veri köküne aitti
        if update.active_project_id is not None:
            if update.active_project_id == "":
                settings.active_project_id = None
            elif get_project(effective_data_root(settings), update.active_project_id) is None:
                raise _error(404, "project_not_found")
            else:
                settings.active_project_id = update.active_project_id
        save_settings(settings)
        return _settings_view(settings)

    @app.get("/api/projects", response_model=list[Project])
    def read_projects() -> list[Project]:
        return list_projects(effective_data_root(load_settings()))

    @app.post("/api/projects", response_model=Project, status_code=201)
    def add_project(payload: ProjectCreate) -> Project:
        settings = load_settings()
        data_root = effective_data_root(settings)
        _check_writable(data_root)
        try:
            project = create_project(data_root, payload)
        except ProjectError as exc:
            raise _error(409, exc.code) from exc
        settings.active_project_id = project.id
        save_settings(settings)
        return project

    @app.get("/api/projects/{project_id}", response_model=Project)
    def read_project(project_id: str) -> Project:
        project = get_project(effective_data_root(load_settings()), project_id)
        if project is None:
            raise _error(404, "project_not_found")
        return project

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def api_not_found(rest: str) -> None:
        raise _error(404, "not_found")

    if static_dir is not None and (static_dir / "index.html").is_file():
        root = static_dir.resolve()

        @app.get("/{path:path}", include_in_schema=False)
        def ui(path: str) -> FileResponse:
            candidate = (root / path).resolve()
            if path and candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
            return FileResponse(root / "index.html")  # tek sayfa uygulama yönlendirmesi

    return app
