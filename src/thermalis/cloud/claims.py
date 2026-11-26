"""Uzun işlemler için yenilenebilir talepler (claim) ve kalp atışlı yerel kilit.

Uzak talep (nesne deposunda, ``state/...``):
- Alma: yalnız-oluştur (``If-None-Match: *``). Aynı işlem kimliği (belirlenimci) kendi yarım işini sürdürür.
- Yenileme: ``heartbeat_at`` alanı koşullu güncellemeyle (``If-Match``) en çok ``renew_every`` aralıkla yazılır.
- Süre aşımı: son kalp atışından (yoksa alma zamanından) ``ttl`` geçmişse başka bir işlemci koşullu olarak devralır
  (yarışta tek kazanan). Devralınan talebin sahibi bir sonraki yenilemede :class:`ClaimLost` alır ve DURUR.
- Sonuç (ACCEPTED/REJECTED) aynı nesneye koşullu yazılır.

Yerel kilit (aynı bilgisayardaki süreçler): ``O_EXCL`` ile oluşturulan dosya; sahibi düzenli dokunur (mtime).
``stale_seconds`` boyunca dokunulmamış kilit çökmüş süreç sayılır ve kaldırılır — uzun aktarımlar kilidi
canlı tuttuğu için 12 saatlik talep süresini beklemek gerekmez.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..objectstore.base import ObjectStore
from ..objectstore.errors import AlreadyExists, PreconditionFailed

TERMINAL = ("ACCEPTED", "REJECTED")


class Busy(Exception):
    pass


class ClaimLost(Exception):
    """Talep başka bir işlemciye geçti (süre aşımı ve devralma); bu işlem yazmayı bırakmalıdır."""


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _canonical(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


class RenewableClaim:
    def __init__(self, store: ObjectStore, key: str, *, operation_id: str, machine: str, schema: str, ttl: timedelta,
                 renew_every: timedelta | None = None, now: Callable[[], datetime] | None = None,
                 extra: dict[str, Any] | None = None) -> None:
        self.store, self.key, self.op, self.machine, self.schema = store, key, operation_id, machine, schema
        self.ttl = ttl
        self.renew_every = renew_every or min(timedelta(minutes=5), ttl / 4)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.extra = dict(extra or {})
        self.etag: str | None = None
        self._last_renew: datetime | None = None
        self.claimed_at: str | None = None

    def _read(self) -> tuple[dict[str, Any], str]:
        head = self.store.head(self.key)
        if head is None:
            return {}, ""
        with self.store.open_read(self.key) as handle:
            raw = handle.read()
        try:
            return json.loads(raw.decode("utf-8")), head.etag
        except (UnicodeDecodeError, ValueError):
            return {"status": "CORRUPT"}, head.etag

    def _body(self, status: str, **fields: Any) -> dict[str, Any]:
        t = _iso(self.now())
        return {"schema": self.schema, "status": status, "operation_id": self.op, "machine": self.machine,
                "claimed_at": self.claimed_at or t, "heartbeat_at": t, **self.extra, **fields}

    def acquire(self) -> tuple[str, dict[str, Any] | None]:
        """("claimed", None) | ("terminal", durum). Başkası tutuyorsa :class:`Busy`."""
        if self.store.head(self.key) is None:
            try:
                info = self.store.put_bytes(self.key, _canonical(self._body("PROCESSING")), create_only=True, operation_id=self.op)
                self.etag, self._last_renew = info.etag, self.now()
                self.claimed_at = _iso(self._last_renew)
                return "claimed", None
            except AlreadyExists:
                pass
        current, etag = self._read()
        if current.get("status") in TERMINAL:
            return "terminal", current
        if current.get("operation_id") == self.op:
            self.claimed_at = current.get("claimed_at")
            self.etag = etag
            self.renew(force=True)  # kendi yarım işimiz: sürdür (kalp atışı tazelenir)
            return "claimed", None
        last = max(_parse(current.get("heartbeat_at")), _parse(current.get("claimed_at")))
        if self.now() - last < self.ttl:
            raise Busy(str(current.get("machine") or "other"))
        try:
            body = self._body("PROCESSING", taken_over_from=current.get("operation_id"))
            body["claimed_at"] = body["heartbeat_at"]
            info = self.store.put_bytes(self.key, _canonical(body), if_match=etag, operation_id=self.op)
        except PreconditionFailed:
            raise Busy("contended") from None
        self.etag, self._last_renew, self.claimed_at = info.etag, self.now(), body["claimed_at"]
        return "claimed", None

    def renew(self, force: bool = False) -> None:
        """Kalp atışı. Çağrılması ucuzdur: yalnız ``renew_every`` geçmişse ağ isteği yapılır."""
        now = self.now()
        if not force and self._last_renew is not None and now - self._last_renew < self.renew_every:
            return
        if self.etag is None:
            _, self.etag = self._read()
        try:
            info = self.store.put_bytes(self.key, _canonical(self._body("PROCESSING")), if_match=self.etag, operation_id=self.op)
        except PreconditionFailed:
            current, etag = self._read()
            if current.get("operation_id") != self.op or current.get("status") in TERMINAL:
                raise ClaimLost(str(current.get("operation_id"))) from None
            self.etag = etag  # kendi önceki (belirsiz) yazımımız
            info = self.store.put_bytes(self.key, _canonical(self._body("PROCESSING")), if_match=etag, operation_id=self.op)
        self.etag, self._last_renew = info.etag, now

    def finish(self, status: str, **fields: Any) -> dict[str, Any]:
        body = {"schema": self.schema, "status": status, "operation_id": self.op, "machine": self.machine,
                "finished_at": _iso(self.now()), **fields}
        current, etag = self._read()
        if current and current.get("operation_id") not in (self.op, None) and current.get("status") not in TERMINAL:
            raise ClaimLost(str(current.get("operation_id")))
        if not etag:
            self.store.put_bytes(self.key, _canonical(body), create_only=True, operation_id=self.op)
        else:
            self.store.put_bytes(self.key, _canonical(body), if_match=etag, operation_id=self.op)
        return body


class LocalLock:
    """Süreçler arası yerel kilit; sahibi :meth:`touch` ile canlı tutar."""

    def __init__(self, path: Path, *, stale_seconds: float = 1800.0, touch_every: float = 30.0) -> None:
        self.path, self.stale_seconds, self.touch_every = Path(path), stale_seconds, touch_every
        self._last = 0.0
        self.held = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    continue
                if age < self.stale_seconds:
                    return False
                self.path.unlink(missing_ok=True)  # kalp atışı kesilmiş (çökmüş) sahip
                continue
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            self.held, self._last = True, time.time()
            return True
        return False

    def touch(self) -> None:
        if self.held and time.time() - self._last >= self.touch_every:
            try:
                os.utime(self.path)
            except OSError:
                pass
            self._last = time.time()

    def release(self) -> None:
        if self.held:
            self.path.unlink(missing_ok=True)
            self.held = False

    def __enter__(self) -> "LocalLock":
        if not self.acquire():
            raise Busy(f"local_lock_held:{self.path.name}")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()
