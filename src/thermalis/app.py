"""THERMALIS yerel uygulama sunucusu.

Yalnız ``127.0.0.1`` üzerinde çalışır. ``/api`` altında JSON servisleri,
geri kalan yollarda derlenmiş arayüz sunulur.
Hatalar arayüzün çevirebilmesi için makine okunur ``code`` ile döner.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

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


class SemanticVerification(BaseModel):
    """Yetkili kullanıcının alan anlamı doğrulaması; boş bırakılan öznitelik değiştirilmez."""
    dataset_id: str
    version: str
    field: str
    meaning: str | None = Field(default=None, max_length=2000)
    description: str | None = Field(default=None, max_length=4000)
    unit: str | None = Field(default=None, max_length=64)
    unit_system: str | None = Field(default=None, max_length=64)
    null_meaning: str | None = Field(default=None, max_length=1000)
    valid_min: float | None = None
    valid_max: float | None = None
    scale: float | None = None
    offset: float | None = None
    note: str | None = Field(default=None, max_length=2000)
    verified_by: str | None = Field(default=None, max_length=200)


ALLOWED_HOSTS_DEFAULT = "127.0.0.1,localhost"
TOKEN_HEADER = "X-Thermalis-Component-Token"
UI_COOKIE = "thermalis_ui_session"
UI_CODE_TTL_SECONDS = 600

# Arayüz oturumu: `thermalis start` tarayıcıyı tek kullanımlık bir kodla açar; kod HttpOnly, SameSite=Strict bir
# oturum çerezine çevrilir. Yalnız bellekte tutulur (yeniden başlatmada düşer). Bu bir kullanıcı kimliği değil,
# isteğin bu bilgisayarda programı başlatan kullanıcının tarayıcısından geldiğinin kanıtıdır.
_UI_CODES: dict[str, float] = {}
_UI_SESSIONS: set[str] = set()
_UI_LOCK = threading.Lock()


def issue_ui_code() -> str:
    code = secrets.token_urlsafe(24)
    with _UI_LOCK:
        now = time.monotonic()
        for k in [k for k, exp in _UI_CODES.items() if exp < now]:
            _UI_CODES.pop(k, None)
        _UI_CODES[code] = now + UI_CODE_TTL_SECONDS
    return code


class SessionClaim(BaseModel):
    code: str = Field(max_length=200)


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
    # Yalnız yerel ad: başka bir sitenin DNS yeniden bağlama ile yerel API'yi okumasını engeller.
    hosts = [h.strip() for h in os.environ.get("THERMALIS_ALLOWED_HOSTS", ALLOWED_HOSTS_DEFAULT).split(",") if h.strip()]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    def authorized(request: Request) -> bool:
        from .settings import component_token

        supplied = request.headers.get(TOKEN_HEADER, "")
        return bool(supplied) and secrets.compare_digest(supplied, component_token())

    def ui_session(request: Request) -> bool:
        value = request.cookies.get(UI_COOKIE, "")
        with _UI_LOCK:
            return bool(value) and value in _UI_SESSIONS

    def verification_channel(request: Request) -> str | None:
        """Anlamsal doğrulama yazabilecek yerel kanal; yoksa None (istek reddedilir)."""
        if authorized(request):
            return "component_token"
        if ui_session(request):
            return "ui_session"
        return None

    @app.get("/api/session")
    def session_status(request: Request) -> dict:
        channel = verification_channel(request)
        return {"verification_allowed": channel is not None, "channel": channel, "identity_verified": False}

    @app.post("/api/session/claim")
    def session_claim(payload: SessionClaim, response: Response) -> dict:
        with _UI_LOCK:
            expiry = _UI_CODES.pop(payload.code, None)
            if expiry is None or expiry < time.monotonic():
                raise _error(403, "session_code_invalid")
            session = secrets.token_urlsafe(32)
            _UI_SESSIONS.add(session)
        response.set_cookie(UI_COOKIE, session, httponly=True, samesite="strict", path="/api", secure=False)
        return {"verification_allowed": True, "channel": "ui_session", "identity_verified": False}

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

    # ---- Bilimsel veri erişim sözleşmesi (v1) -----------------------------------------------------
    # Diğer THERMALIS bileşenleri için kararlı, sürümlü uçlar. İş mantığı data/contract.py'dedir.
    def _contract(call):
        from .data.contract import NotFound

        try:
            return call()
        except NotFound as exc:
            raise _error(404, exc.code) from exc
        except ValueError as exc:  # geçersiz kimlik biçimi
            raise _error(400, "invalid_dataset_reference") from exc

    def _root() -> Path:
        return effective_data_root(load_settings())

    @app.get("/api/data/v1/datasets")
    def v1_datasets() -> list[dict]:
        from .data import contract

        return _contract(lambda: contract.list_datasets(_root()))

    @app.get("/api/data/v1/versions")
    def v1_versions(id: str) -> list[dict]:
        from .data import contract

        return _contract(lambda: contract.list_versions(_root(), id))

    @app.get("/api/data/v1/descriptor")
    def v1_descriptor(request: Request, id: str, version: str | None = None) -> dict:
        from .data import contract

        return _contract(lambda: contract.descriptor(_root(), id, version, authorized=authorized(request)))

    @app.get("/api/data/v1/semantics")
    def v1_semantics(id: str, version: str | None = None) -> dict:
        from .data import contract

        return _contract(lambda: contract.semantic_dictionary(_root(), id, version))

    @app.get("/api/data/v1/field")
    def v1_field(id: str, name: str, version: str | None = None) -> dict:
        from .data import contract

        return _contract(lambda: contract.field_info(_root(), id, version, name))

    @app.get("/api/data/v1/domain")
    def v1_domain(id: str, name: str, version: str | None = None) -> dict:
        from .data import contract

        return _contract(lambda: contract.domain_codes(_root(), id, version, name))

    @app.get("/api/data/v1/qa")
    def v1_qa(id: str, version: str | None = None) -> dict:
        from .data import contract

        return _contract(lambda: contract.quality(_root(), id, version))

    @app.get("/api/data/v1/lineage")
    def v1_lineage(request: Request, id: str, version: str | None = None) -> dict:
        from .data import contract

        return _contract(lambda: contract.lineage(_root(), id, version, authorized=authorized(request)))

    @app.get("/api/data/v1/access")
    def v1_access(request: Request, id: str, version: str | None = None) -> dict:
        from .data import contract

        return _contract(lambda: contract.access(_root(), id, version, authorized=authorized(request)))

    @app.get("/api/data/v1/diff")
    def v1_diff(id: str, to: str, from_: str = Query(alias="from")) -> dict:
        from .data import contract

        return _contract(lambda: contract.diff(_root(), id, from_, to))

    @app.post("/api/data/v1/semantics/verify")
    def v1_verify_semantics(payload: SemanticVerification, request: Request) -> dict:
        from .data import contract, semantics
        from .data.catalog_index import rebuild_index

        channel = verification_channel(request)
        if channel is None:
            raise _error(403, "verification_not_authorized")
        root = _root()
        current = _contract(lambda: contract.field_info(root, payload.dataset_id, payload.version, payload.field))  # var olmalı
        if current["version"] != payload.version:
            # Paket sürümüyle çözülen istekler doğrulama yazamaz: doğrulama yalnız tam dataset sürümüne bağlanır.
            raise _error(409, "verification_version_mismatch")
        if payload.meaning is None and all(getattr(payload, k) is None for k in semantics.VERIFIABLE if k != "meaning"):
            raise _error(400, "validation")
        previous = {k: current.get(k) for k in (*semantics.VERIFIABLE, "status", "origin", "unit_status")}
        try:
            semantics.append_verification(root, payload.model_dump(exclude_none=True), auth_method=channel,
                                          content_id=current.get("content_id"), previous=previous)
        except ValueError as exc:
            raise _error(409, "verification_log_broken") from exc
        except TimeoutError as exc:
            raise _error(503, "verification_log_busy") from exc
        rebuild_index(root)
        return _contract(lambda: contract.field_info(root, payload.dataset_id, payload.version, payload.field))

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
