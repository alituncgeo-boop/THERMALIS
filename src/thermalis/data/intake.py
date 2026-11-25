"""Gelen paket klasörünün otomatik izlenmesi (uygulama içinde, aralıklı tarama).

İşletim sistemi dosya olaylarına bağlı değildir: uygulama açıkken ayarlardaki aralıkla
klasörü tarar, açılışta hemen bir tarama yapar (uygulama kapalıyken bırakılan paketler
böylece algılanır). Ayrı bir servis veya süreç yoktur; uygulamanın yaşam döngüsüyle
başlar ve durur.

Her tarama :func:`ingest.run_ingest` ile yapılır (``require_stable=True``,
``retry_failed=False``). Böylece komut satırıyla aynı kapılar, aynı kalıcı durum
(``_ingest/intake.json``) ve aynı kilitler kullanılır.
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..settings import Settings, effective_data_root, load_settings
from .ingest import IngestLocked, log_event, run_ingest

MIN_INTERVAL_SECONDS = 10


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class WatcherStatus:
    enabled: bool = False
    running: bool = False
    incoming: str | None = None
    interval_seconds: float | None = None
    settle_seconds: float | None = None
    last_scan_started: str | None = None
    last_scan_finished: str | None = None
    last_result: str | None = None  # ok | busy | not_configured | incoming_missing | error
    last_error: str | None = None
    scans: int = 0
    processed: int = 0


@dataclass
class CloudStatus:
    enabled: bool = False
    configured: bool = False
    interval_seconds: float | None = None
    last_scan_started: str | None = None
    last_scan_finished: str | None = None
    last_result: str | None = None  # ok | offline | error | not_configured | disabled | credentials_missing | busy
    last_error: str | None = None
    discovered: int = 0
    last_outcomes: list = None  # type: ignore[assignment]
    scans: int = 0


class IntakeWatcher:
    def __init__(self, settings_loader: Callable[[], Settings] = load_settings,
                 cloud_factory: Callable[[Settings, Path], object] | None = None) -> None:
        self._load = settings_loader
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._cloud_now = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = WatcherStatus()
        self.cloud = CloudStatus(last_outcomes=[])
        self._cloud_factory = cloud_factory
        self._next_cloud = 0.0
        self._last_logged_error: str | None = None

    # -- yaşam döngüsü ---------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="thermalis-intake", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def trigger(self, cloud: bool = False) -> None:
        """Hemen tara (arayüzdeki "şimdi tara"). ``cloud=True``: bulut taraması/eşitlemesi de hemen yapılır."""
        if cloud:
            self._cloud_now.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            interval = self.scan_once()
            try:
                interval = min(interval, self.cloud_scan_if_due())
            except Exception:  # pragma: no cover - bulut taraması izleyiciyi asla durdurmaz
                pass
            self._wake.wait(timeout=interval)
            self._wake.clear()

    # -- bulut taraması (ayrı, daha seyrek aralık: istek maliyeti sınırlı) ------------------------
    def cloud_scan_if_due(self) -> float:
        import time

        settings = self._load()
        c = self.cloud
        ci = settings.cloud_intake
        c.enabled = bool(ci.enabled)
        c.configured = bool(settings.cloud.endpoint and settings.cloud.bucket)
        c.interval_seconds = float(ci.interval_seconds)
        if not c.enabled:
            c.last_result = "disabled"
            return float(ci.interval_seconds)
        if not c.configured:
            c.last_result = "not_configured"
            return float(ci.interval_seconds)
        now = time.monotonic()
        if now < self._next_cloud and not self._cloud_now.is_set():
            return max(self._next_cloud - now, 1.0)
        self._cloud_now.clear()
        self.cloud_scan_once(settings)
        self._next_cloud = time.monotonic() + ci.interval_seconds
        return float(ci.interval_seconds)

    def cloud_scan_once(self, settings: Settings | None = None) -> None:
        from ..objectstore.errors import StorageError

        settings = settings or self._load()
        data_root = effective_data_root(settings)
        c = self.cloud
        c.last_scan_started = _utc_now()
        try:
            if self._cloud_factory is not None:
                processor = self._cloud_factory(settings, data_root)
            else:
                from ..cloud.intake import cloud_intake_from_settings

                processor = cloud_intake_from_settings(settings, data_root)
            result = processor.scan()
            c.last_result, c.last_error, c.discovered = result.status, result.error, result.discovered
            c.last_outcomes = [o.__dict__ for o in result.outcomes][-20:]
            for o in result.outcomes:
                if o.status in ("ACCEPTED", "REJECTED", "DEFERRED"):
                    log_event(data_root, event="cloud_package", package_id=o.package_id, status=o.status, code=o.code,
                              dataset_id=o.dataset_id, version=o.version)
        except StorageError as exc:
            c.last_result = "credentials_missing" if exc.code == "credentials_missing" else ("offline" if exc.retryable else "error")
            c.last_error = exc.code
        except Exception as exc:  # izleyici asla çökmez; ayrıntı arayüzde görünür
            c.last_result, c.last_error = "error", type(exc).__name__
        finally:
            c.scans += 1
            c.last_scan_finished = _utc_now()
        if c.last_result in ("error", "offline", "credentials_missing") and c.last_error != self._last_logged_error:
            self._last_logged_error = c.last_error  # aynı hata her taramada yeniden günlüğe yazılmaz
            try:
                log_event(data_root, event="cloud_scan_problem", result=c.last_result, error=c.last_error)
            except OSError:
                pass
        elif c.last_result == "ok":
            self._last_logged_error = None

    # -- tek tarama ------------------------------------------------------------------------------
    def scan_once(self) -> float:
        """Bir tarama yapar; bir sonraki taramaya kadar beklenecek süreyi döndürür."""
        settings = self._load()
        interval = max(float(settings.transfer_scan_interval_seconds), MIN_INTERVAL_SECONDS)
        s = self.status
        s.enabled = bool(settings.transfer_watch_enabled and settings.transfer_incoming_dir)
        s.incoming = settings.transfer_incoming_dir
        s.interval_seconds = interval
        s.settle_seconds = float(settings.transfer_settle_seconds)
        if not settings.transfer_watch_enabled or not settings.transfer_incoming_dir:
            s.last_result, s.last_error = "not_configured", None
            return interval
        incoming = Path(settings.transfer_incoming_dir)
        data_root = effective_data_root(settings)
        s.running, s.last_scan_started = True, _utc_now()
        try:
            if not incoming.is_dir():
                s.last_result, s.last_error = "incoming_missing", str(incoming)
                return interval
            summary = run_ingest(incoming, data_root, settle_seconds=s.settle_seconds, require_stable=True,
                                 retry_failed=False, wait=False)
            s.processed += len(summary.results)
            s.last_result, s.last_error = "ok", None
        except IngestLocked:
            s.last_result, s.last_error = "busy", None  # başka bir entegrasyon sürüyor; sonraki taramada denenir
        except Exception as exc:  # izleyici asla çökmez; hata arayüzde görünür
            repeated = s.last_result == "error" and s.last_error == f"{type(exc).__name__}: {exc}"
            s.last_result, s.last_error = "error", f"{type(exc).__name__}: {exc}"
            if not repeated:  # aynı hata her taramada yeniden günlüğe yazılmaz
                try:
                    log_event(data_root, event="watcher_error", error=s.last_error)
                except OSError:
                    pass
        finally:
            s.running = False
            s.scans += 1
            s.last_scan_finished = _utc_now()
        return interval

    def as_dict(self) -> dict:
        return {**asdict(self.status), "cloud": asdict(self.cloud)}
