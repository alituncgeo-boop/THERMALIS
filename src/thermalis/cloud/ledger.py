"""Bulut defteri (ledger): yetkili, değişmez, yalnız-eklenen küçük kayıtlar (R2 + yerel yansı).

Değişmez sürüm içeriğine dokunmadan kalıcı olması gereken bilimsel ve işletimsel kayıtlar burada tutulur:

    state/ledger/metadata_revisions/<kimlik>.json     metadata düzeltmeleri (denetlenebilir; yük yeniden yüklenmez)
    state/ledger/authorizations/<kimlik>.json         operatör yetkileri ve geri almalar
    state/ledger/semantic_verifications/<sıra>-<özet>.json   anlamsal doğrulama zinciri (yerel günlüğün kopyası)
    state/ledger/reverifications/<özet>.json          yeniden doğrulama geçmişi
    state/ledger/authority/<kimlik>.json              sürüm başına yetkili kaynak kararları (r2 | local; geri alma dahil)

Kurallar:
- Her girdi yalnız-oluştur (``If-None-Match: *``) yazılır; aynı kimlikle farklı içerik çakışmadır, üzerine yazılmaz.
- Girdi kimliği içerikten türetilir (SHA-256): aynı kayıt iki kez yazılmaz, sıra makinelerden bağımsızdır.
- Yerel yansı ``<veri kökü>/data/_cloud/ledger/<akış>/`` türetilmiştir; silinirse buluttan aynen kurulur.
- Çevrimdışı yazılan girdi yerelde ``pending`` kalır ve sonraki eşitlemede itilir.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from ..objectstore.base import ObjectStore
from ..objectstore.errors import AlreadyExists, StorageError
from ..storage import write_json_atomic

PREFIX = "state/ledger/"
STREAMS = ("metadata_revisions", "authorizations", "semantic_verifications", "reverifications", "authority")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


class LedgerError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def canonical(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def content_id(body: dict[str, Any], length: int = 24) -> str:
    """İçerikten türetilen kimlik (``id`` alanı hariç)."""
    clean = {k: v for k, v in body.items() if k not in ("id",)}
    return hashlib.sha256(json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:length]


def ledger_dir(data_root: Path, stream: str) -> Path:
    from ..data import catalog

    if stream not in STREAMS:
        raise LedgerError("unknown_stream", stream)
    return catalog.data_dir(data_root) / "_cloud" / "ledger" / stream


def pending_dir(data_root: Path, stream: str) -> Path:
    return ledger_dir(data_root, stream).parent / "_pending" / stream


def _key(stream: str, entry_id: str) -> str:
    if stream not in STREAMS or not _ID.match(entry_id or ""):
        raise LedgerError("invalid_ledger_entry", f"{stream}/{entry_id}")
    return f"{PREFIX}{stream}/{entry_id}.json"


def record_local(data_root: Path, stream: str, entry_id: str, body: dict[str, Any], *, pending: bool = True) -> Path:
    """Girdiyi yerel yansıya (ve itilecekler kuyruğuna) yazar. Aynı kimlik farklı içerikse reddedilir."""
    _key(stream, entry_id)
    target = ledger_dir(data_root, stream) / f"{entry_id}.json"
    if target.is_file():
        if json.loads(target.read_text(encoding="utf-8")) != body:
            raise LedgerError("ledger_entry_conflict", f"{stream}/{entry_id}")
        return target
    write_json_atomic(target, body)
    if pending:
        write_json_atomic(pending_dir(data_root, stream) / f"{entry_id}.json", body)
    return target


def push(store: ObjectStore, stream: str, entry_id: str, body: dict[str, Any]) -> str:
    """Girdiyi buluta yalnız-oluştur yazar. Aynı içerik zaten varsa başarı; farklı içerik çakışma."""
    key = _key(stream, entry_id)
    data = canonical(body)
    # İşlem kimliği içerikten türetilir: aynı içerik yeniden denenirse eşgüçlü; farklı içerik asla üzerine yazamaz.
    operation_id = "ledger-" + hashlib.sha256(data).hexdigest()[:24]
    try:
        store.put_bytes(key, data, create_only=True, operation_id=operation_id)
        return "written"
    except AlreadyExists:
        with store.open_read(key) as handle:
            if json.loads(handle.read().decode("utf-8")) == body:
                return "exists"
        raise LedgerError("ledger_entry_conflict", key) from None


def push_pending(store: ObjectStore, data_root: Path) -> dict[str, int]:
    """Çevrimdışı biriken yerel girdileri iter; başarıyla itilen kuyruktan çıkar."""
    out = {"pushed": 0, "conflicts": 0}
    for stream in STREAMS:
        folder = pending_dir(data_root, stream)
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
            body = json.loads(path.read_text(encoding="utf-8"))
            try:
                push(store, stream, path.stem, body)
                out["pushed"] += 1
                path.unlink(missing_ok=True)
            except LedgerError:
                out["conflicts"] += 1
    return out


def pull(store: ObjectStore, data_root: Path, streams: Iterable[str] = STREAMS) -> dict[str, int]:
    """Buluttaki girdileri yerel yansıya indirir (yalnız eksik olanlar; listeleme önek başına sayfalı)."""
    out = {"pulled": 0, "known": 0, "invalid": 0}
    for stream in streams:
        folder = ledger_dir(data_root, stream)
        for info in store.list(f"{PREFIX}{stream}/"):
            entry_id = info.key.rsplit("/", 1)[-1][: -len(".json")] if info.key.endswith(".json") else None
            if not entry_id or not _ID.match(entry_id):
                out["invalid"] += 1
                continue
            target = folder / f"{entry_id}.json"
            if target.is_file():
                out["known"] += 1
                continue
            with store.open_read(info.key) as handle:
                raw = handle.read()
            if info.sha256 and hashlib.sha256(raw).hexdigest() != info.sha256:
                out["invalid"] += 1
                continue
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                out["invalid"] += 1
                continue
            write_json_atomic(target, body)
            out["pulled"] += 1
    return out


def entries(data_root: Path, stream: str) -> list[dict[str, Any]]:
    folder = ledger_dir(data_root, stream)
    out = []
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(body, dict):
            out.append({**body, "_ledger_id": path.stem})
    return out


def sync(store: ObjectStore, data_root: Path) -> dict[str, Any]:
    """Defter eşitlemesi: yerel denetim günlüklerini deftere aktar, bekleyenleri it, buluttakileri çek,
    eksik yerel günlük satırlarını defterden geri kur. Hata olursa katalog eşitlemesini durdurmaz."""
    from .audit_logs import export_local_logs, restore_local_logs

    exported = export_local_logs(data_root)
    pushed = push_pending(store, data_root)
    pulled = pull(store, data_root)
    restored = restore_local_logs(data_root)
    return {"exported": exported, **pushed, **pulled, "restored": restored}


def safe_sync(store: ObjectStore, data_root: Path) -> dict[str, Any]:
    try:
        return {"status": "ok", **sync(store, data_root)}
    except (StorageError, LedgerError, OSError) as exc:
        return {"status": "error", "error": getattr(exc, "code", type(exc).__name__)}
