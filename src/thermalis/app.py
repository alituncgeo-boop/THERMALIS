"""THERMALIS yerel uygulama sunucusu.

Yalnız ``127.0.0.1`` üzerinde çalışır. ``/api`` altında JSON servisleri,
geri kalan yollarda derlenmiş arayüz sunulur.
Hatalar arayüzün çevirebilmesi için makine okunur ``code`` ile döner.
"""

from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

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
    "data": "in_progress",
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
    transfer_incoming_dir: str | None
    transfer_watch_enabled: bool
    transfer_scan_interval_seconds: int
    transfer_settle_seconds: int


class SettingsUpdate(BaseModel):
    language: Language | None = None
    data_root: str | None = None
    active_project_id: str | None = None
    transfer_incoming_dir: str | None = None
    transfer_watch_enabled: bool | None = None
    transfer_scan_interval_seconds: int | None = Field(default=None, ge=10, le=3600)
    transfer_settle_seconds: int | None = Field(default=None, ge=0, le=86400)


class IntakeSubmit(BaseModel):
    path: str
    expect_dataset_id: str | None = None


class IntakeRetry(BaseModel):
    key: str


class VerifyRequest(BaseModel):
    dataset_id: str
    version: str


def _error(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code})


def _settings_view(settings: Settings) -> SettingsView:
    return SettingsView(
        language=settings.language,
        data_root=str(effective_data_root(settings)),
        data_root_is_default=settings.data_root is None,
        active_project_id=settings.active_project_id,
        transfer_incoming_dir=settings.transfer_incoming_dir,
        transfer_watch_enabled=settings.transfer_watch_enabled,
        transfer_scan_interval_seconds=settings.transfer_scan_interval_seconds,
        transfer_settle_seconds=settings.transfer_settle_seconds,
    )


def _check_writable(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".thermalis_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise _error(400, "data_root_not_writable") from exc


def create_app(static_dir: Path | None = None, *, watch: bool | None = None) -> FastAPI:
    """``watch``: gelen klasör izleyicisi uygulamayla başlasın mı (varsayılan: ``THERMALIS_DISABLE_WATCHER`` yoksa evet)."""
    from .data.intake import IntakeWatcher

    watcher = IntakeWatcher()
    if watch is None:
        watch = os.environ.get("THERMALIS_DISABLE_WATCHER") != "1"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if watch:
            watcher.start()  # açılışta hemen tarar: uygulama kapalıyken bırakılan paketler de algılanır
        try:
            yield
        finally:
            watcher.stop()

    app = FastAPI(title="THERMALIS", version=__version__, docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)
    app.state.watcher = watcher

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
        if update.transfer_incoming_dir is not None:
            raw = update.transfer_incoming_dir.strip()
            if raw == "":
                settings.transfer_incoming_dir = None
            else:
                folder = Path(raw)
                if not folder.is_absolute():
                    raise _error(400, "path_not_absolute")
                if not folder.is_dir():
                    raise _error(400, "path_not_found")
                settings.transfer_incoming_dir = str(folder)
        if update.transfer_watch_enabled is not None:
            settings.transfer_watch_enabled = update.transfer_watch_enabled
        if update.transfer_scan_interval_seconds is not None:
            settings.transfer_scan_interval_seconds = update.transfer_scan_interval_seconds
        if update.transfer_settle_seconds is not None:
            settings.transfer_settle_seconds = update.transfer_settle_seconds
        save_settings(settings)
        watcher.trigger()  # yeni ayarlar bir sonraki taramayı beklemeden uygulanır
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

    @app.get("/api/data/versions")
    def data_versions() -> list[dict]:
        from .data.catalog import list_versions

        return list_versions(effective_data_root(load_settings()))

    @app.get("/api/data/version")
    def data_version(dataset_id: str, version: str) -> dict:
        from .data.catalog import read_version

        try:
            found = read_version(effective_data_root(load_settings()), dataset_id, version)
        except ValueError as exc:
            raise _error(400, "invalid_dataset_reference") from exc
        if found is None:
            raise _error(404, "dataset_not_found")
        record = found["record"]
        # Arayüze dosya bazlı özetler değil, özet bütünlük bilgisi gider.
        integrity = found["integrity"]
        return {
            "record": record,
            "quality": found["quality"],
            "integrity": {k: v for k, v in integrity.items() if k != "files"},
        }

    @app.get("/api/data/layers")
    def data_layers(dataset_id: str | None = None, version: str | None = None) -> list[dict]:
        from .data.catalog_index import query_layers

        return query_layers(effective_data_root(load_settings()), dataset_id, version)

    @app.get("/api/data/findings")
    def data_findings(dataset_id: str, version: str, category: str | None = None, layer_id: str | None = None) -> dict:
        from .data.catalog_index import finding_summary, query_findings

        root = effective_data_root(load_settings())
        return {"summary": finding_summary(root, dataset_id, version),
                "findings": query_findings(root, dataset_id, version, category=category, layer_id=layer_id)}

    @app.get("/api/data/formats")
    def data_formats() -> list[dict]:
        from .data.adapters import support_matrix

        return support_matrix()

    # ---- Dataset Catalog: mantıksal veri setleri, tam metadata, dışa aktarım -------------------
    @app.get("/api/data/catalog")
    def data_catalog() -> dict:
        from .data.views import catalog_items

        return {"datasets": catalog_items(effective_data_root(load_settings()))}

    @app.get("/api/data/dataset")
    def data_dataset(id: str, version: str | None = None) -> dict:
        from .data.views import dataset_metadata

        found = dataset_metadata(effective_data_root(load_settings()), id, version)
        if found is None:
            raise _error(404, "dataset_not_found")
        return found

    @app.get("/api/data/export")
    def data_export(format: str = "json") -> Response:
        from .data.views import export_catalog

        if format not in ("json", "csv"):
            raise _error(400, "validation")
        content, media = export_catalog(effective_data_root(load_settings()), format)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return Response(content=content.encode("utf-8"), media_type=f"{media}; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="thermalis_catalog_{stamp}.{format}"'})

    @app.post("/api/data/verify")
    def data_verify(payload: VerifyRequest) -> dict:
        from .data.catalog_index import rebuild_index
        from .data.register import verify_registered

        root = effective_data_root(load_settings())
        try:
            checks = verify_registered(root, payload.dataset_id, payload.version)
        except (ValueError, FileNotFoundError) as exc:
            raise _error(404, "dataset_not_found") from exc
        rebuild_index(root)
        return {"status": "PASS" if all(c.status == "PASS" for c in checks) else "FAIL", "checks": [c.as_dict() for c in checks]}

    # ---- Otomatik paket kabulü --------------------------------------------------------------------
    @app.get("/api/data/intake")
    def data_intake() -> dict:
        from .data.catalog_index import _query, index_path
        from .data.ingest import INTAKE_STATES, load_intake

        root = effective_data_root(load_settings())
        packages = sorted(load_intake(root)["packages"].values(), key=lambda e: e.get("updated_at") or "", reverse=True)
        counts = {s: sum(1 for p in packages if p.get("state") == s and p.get("present", True)) for s in INTAKE_STATES}
        runs = _query(root, "SELECT * FROM ingest_runs ORDER BY finished_at DESC LIMIT 10") if index_path(root).is_file() else []
        return {"watcher": watcher.as_dict(), "packages": packages, "counts": counts, "recent_runs": runs}

    @app.post("/api/data/intake/scan")
    def data_intake_scan() -> dict:
        watcher.trigger()
        return {"triggered": True, "watcher": watcher.as_dict()}

    @app.post("/api/data/intake/submit", status_code=202)
    def data_intake_submit(payload: IntakeSubmit) -> dict:
        from .data.ingest import _is_link, ingest_path
        from .data.transfer_package import PackageError, load_package

        root = effective_data_root(load_settings())
        folder = Path(payload.path.strip())
        if not folder.is_absolute():
            raise _error(400, "path_not_absolute")
        if not folder.is_dir():
            raise _error(400, "path_not_found")
        if _is_link(folder):
            raise _error(400, "path_is_link")
        if payload.expect_dataset_id:
            try:
                declared = load_package(folder).dataset_id
            except (PackageError, OSError, ValueError, KeyError) as exc:
                raise _error(400, "package_invalid") from exc
            if declared != payload.expect_dataset_id:
                raise _error(409, "package_dataset_mismatch")

        def work() -> None:
            try:
                ingest_path(folder, root, expect_dataset_id=payload.expect_dataset_id)
            except Exception:  # durum intake.json ve olay günlüğünde; iş parçacığı sessizce ölmez
                import traceback

                from .data.ingest import log_event

                log_event(root, event="manual_submit_error", package=str(folder), error=traceback.format_exc(limit=3))

        threading.Thread(target=work, name="thermalis-manual-intake", daemon=True).start()
        return {"accepted": True, "path": str(folder)}

    @app.post("/api/data/intake/retry")
    def data_intake_retry(payload: IntakeRetry) -> dict:
        from .data.ingest import forget_failure

        if not forget_failure(effective_data_root(load_settings()), payload.key):
            raise _error(404, "intake_item_not_retryable")
        watcher.trigger()
        return {"retry": True}

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
