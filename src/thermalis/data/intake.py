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


class IntakeWatcher:
    def __init__(self, settings_loader: Callable[[], Settings] = load_settings) -> None:
        self._load = settings_loader
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.status = WatcherStatus()

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

    def trigger(self) -> None:
        """Hemen tara (arayüzdeki "şimdi tara")."""
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            interval = self.scan_once()
            self._wake.wait(timeout=interval)
            self._wake.clear()

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
            s.last_result, s.last_error = "error", f"{type(exc).__name__}: {exc}"
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
        return asdict(self.status)
