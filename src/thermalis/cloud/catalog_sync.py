"""İşaretli bulut sürümlerinin yerel kataloğa eşitlenmesi.

Bulut sürümleri için yetkili kayıt R2'deki ``_COMMIT.json`` ve ``records/*.json`` dosyalarıdır. Yerelde
yalnız TÜRETİLMİŞ bir yansı tutulur (``<veri kökü>/data/_cloud/releases/<SEG>/<SEG>/<sürüm>/``):
kayıt dosyaları, işaretin kopyası, konum bilgisi ve dizinin okuduğu küçük JSON belgeleri. Veri dosyaları
(ör. GDB) indirilmez. Her indirilen dosya işaretteki SHA-256 ile doğrulanır. Yansı silinse bile
:func:`sync_catalog` (``full=True`` ile yalnız işaretlerden) aynen yeniden kurar.

Artımlı eşitleme: ``catalog/commits/`` öneki listelenir (sürüm başına bir küçük nesne); ETag'i değişmemiş
ve yansısı geçerli girdiler indirilmez. İşaretsiz, reddedilmiş veya yarım sürümler hiçbir zaman yansıtılmaz.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..data import catalog
from ..objectstore.base import CancelToken, ObjectStore
from ..objectstore.errors import NotFound, PublicationError, StorageError
from ..objectstore.publication import COMMIT_NAME, committed_releases, read_commit
from ..storage import read_json, write_json_atomic
from . import layout

CLOUD_DIR = "_cloud"
LOCATION_NAME = "CLOUD_LOCATION.json"
LOCATION_SCHEMA = "thermalis.cloud-location/1"
SYNC_STATE_NAME = "sync_state.json"
RECORD_NAMES = ("version.json", "quality.json", "integrity.json")
MAX_DOCUMENT_BYTES = 16 * 1024 * 1024


def cloud_dir(data_root: Path) -> Path:
    return catalog.data_dir(data_root) / CLOUD_DIR


def mirror_root(data_root: Path) -> Path:
    return cloud_dir(data_root) / "releases"


def mirror_folder(data_root: Path, dataset_id: str, version: str) -> Path:
    catalog.version_dir(Path("."), dataset_id, version)  # doğrulama
    return mirror_root(data_root).joinpath(*catalog.dataset_segments(dataset_id), version)


def cloud_record_folders(data_root: Path) -> list[Path]:
    root = mirror_root(data_root)
    if not root.is_dir():
        return []
    return sorted(p.parent for p in root.rglob(LOCATION_NAME) if (p.parent / "version.json").is_file())


def load_sync_state(data_root: Path) -> dict[str, Any]:
    path = cloud_dir(data_root) / SYNC_STATE_NAME
    try:
        data = read_json(path)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


@dataclass
class SyncResult:
    status: str  # ok | offline | error
    mirrored: list[str] = field(default_factory=list)
    unchanged: int = 0
    problems: list[str] = field(default_factory=list)
    release_count: int = 0
    committed_bytes: int = 0
    error: str | None = None


def _drop(folder: Path) -> None:
    for p in folder.rglob("*"):
        if p.is_file():
            try:
                os.chmod(p, stat.S_IREAD | stat.S_IWRITE)
            except OSError:
                pass
    shutil.rmtree(folder, ignore_errors=True)


def _download_verified(store: ObjectStore, key: str, expected: dict[str, Any], dest: Path) -> None:
    import hashlib

    with store.open_read(key) as handle:
        data = handle.read()
    if len(data) != expected["size"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
        raise PublicationError("mirrored_object_does_not_match_commit", key=key)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def mirror_release(store: ObjectStore, data_root: Path, prefix: str, commit: dict[str, Any], *, provider: str,
                   bucket: str, namespace: str = "") -> Path:
    """Bir işaretli sürümün kayıtlarını doğrulayarak yerel yansıya yazar (atomik klasör değişimi)."""
    objects = {o["key"]: o for o in commit["objects"]}
    target = mirror_folder(data_root, commit["dataset_id"], commit["version"])
    staging = mirror_root(data_root) / f".tmp-{uuid.uuid4().hex[:12]}"
    try:
        for name in RECORD_NAMES:
            _download_verified(store, prefix + "records/" + name, objects[prefix + "records/" + name], staging / name)
        record = json.loads((staging / "version.json").read_text(encoding="utf-8"))
        if (record.get("dataset_id"), record.get("version")) != (commit["dataset_id"], commit["version"]) \
                or record.get("registration", {}).get("status") != "REGISTERED":
            raise PublicationError("record_does_not_match_commit", key=prefix)
        # Tüm ek kayıtlar (rapor, içerik kimlikleri, özel kayıt kimlikleri, taşıma kaynağı, denetim ve doğrulama geçmişi)
        # genel olarak yansıtılır; her biri işaretteki SHA-256 ile doğrulanır.
        for name in sorted(k[len(prefix + "records/"):] for k in objects if k.startswith(prefix + "records/")):
            obj = objects[prefix + "records/" + name]
            if name not in RECORD_NAMES and obj["size"] <= MAX_DOCUMENT_BYTES:
                _download_verified(store, prefix + "records/" + name, obj, staging.joinpath(*name.split("/")))
        for doc in record.get("documents", []) or []:
            key = prefix + "payload/" + doc
            obj = objects.get(key)
            if obj and doc.lower().endswith(".json") and obj["size"] <= MAX_DOCUMENT_BYTES:
                _download_verified(store, key, obj, staging.joinpath("payload", *doc.split("/")))
        (staging / COMMIT_NAME).write_text(json.dumps(commit, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
        write_json_atomic(staging / LOCATION_NAME, {
            "schema": LOCATION_SCHEMA, "backend": "r2", "provider": provider, "bucket": bucket, "namespace": namespace,
            "release_prefix": prefix, "committed_at": commit["committed_at"], "operation_id": commit["operation_id"],
            "object_count": len(commit["objects"]), "total_bytes": sum(o["size"] for o in commit["objects"]),
            "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        for p in staging.rglob("*"):
            if p.is_file():
                os.chmod(p, stat.S_IREAD)  # türetilmiş yansı da değişmez kayıt gibi salt okunur
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            _drop(target)
        os.replace(staging, target)
    finally:
        if staging.exists():
            _drop(staging)
    return target


def _identity_from_entry_key(key: str) -> tuple[str, str] | None:
    parts = key[len(layout.CATALOG_COMMITS):].split("/")
    if len(parts) < 2 or not parts[-1].endswith(".json"):
        return None
    dataset_id, version = "THERMALIS_DATA/" + "/".join(parts[:-1]), parts[-1][: -len(".json")]
    try:
        catalog.version_dir(Path("."), dataset_id, version)
    except ValueError:
        return None
    return dataset_id, version


def _location_valid(folder: Path) -> bool:
    return (folder / LOCATION_NAME).is_file() and (folder / "version.json").is_file()


def _location_matches(folder: Path, commit_or_entry: dict[str, Any]) -> bool:
    try:
        loc = read_json(folder / LOCATION_NAME)
    except (OSError, ValueError):
        return False
    return loc.get("operation_id") == commit_or_entry.get("operation_id") and (folder / "version.json").is_file()


def verify_cloud_version(store: ObjectStore, data_root: Path, dataset_id: str, version: str):
    """Bulut sürümünü yeniden doğrular: işaretteki her nesne baştan sona okunur (tam SHA-256). Sonuç günlüğe yazılır."""
    from ..data.register import FAIL, PASS, Check, _log_verification
    from ..objectstore.publication import inspect_release

    loc = read_json(mirror_folder(data_root, dataset_id, version) / LOCATION_NAME)
    state = inspect_release(store, loc["release_prefix"], full=True)
    checks = [Check("INT-R2", "Bulut nesneleri tam SHA-256 doğrulaması (işarete göre)",
                    PASS if state.status == "COMMITTED" else FAIL,
                    f"{state.object_count} nesne · {state.status} {list(state.problems)[:10]}")]
    _log_verification(data_root, dataset_id, version, checks)
    return checks


def sync_catalog(store: ObjectStore, data_root: Path, *, provider: str, bucket: str, namespace: str = "",
                 full: bool = False, cancel: CancelToken | None = None, rebuild: bool = True) -> SyncResult:
    """Bulut kataloğunu yerel yansıya eşitler ve (değişiklik varsa) dizini yeniden kurar.

    ``full=False``: ``catalog/commits/`` girdileri (artımlı). ``full=True``: ``releases/`` altındaki tüm
    ``_COMMIT.json`` işaretleri (katalog girdileri kaybolsa bile tam yeniden kurulum).
    """
    state = load_sync_state(data_root)
    known: dict[str, str] = dict(state.get("entries") or {})
    result = SyncResult("ok")
    seen: dict[str, str] = {}
    try:
        if full:
            sources = []
            for prefix, commit, problem in committed_releases(store):
                if problem or commit is None:
                    result.problems.append(f"{prefix}:{problem}")
                    continue
                sources.append((prefix + COMMIT_NAME, commit.get("operation_id") or "", prefix, commit))
        else:
            sources = []
            for info in store.list(layout.CATALOG_COMMITS):
                # Kimlik anahtardan türetilir: değişmemiş girdi için hiç okuma isteği yapılmaz (artımlı, düşük maliyet).
                ident = _identity_from_entry_key(info.key)
                if ident and known.get(info.key) == info.etag and _location_valid(mirror_folder(data_root, *ident)):
                    seen[info.key] = info.etag
                    result.unchanged += 1
                    continue
                try:
                    with store.open_read(info.key) as handle:
                        entry = json.loads(handle.read().decode("utf-8"))
                except (NotFound, ValueError):
                    result.problems.append(f"{info.key}:unreadable_entry")
                    continue
                sources.append((info.key, info.etag, entry.get("release_prefix"), entry))
        for key, etag, prefix, src in sources:
            if cancel is not None:
                cancel.raise_if_cancelled(key)
            ds, ver = src.get("dataset_id"), src.get("version")
            try:
                folder = mirror_folder(data_root, ds, ver)
            except (ValueError, TypeError):
                result.problems.append(f"{key}:invalid_identity")
                continue
            seen[key] = etag
            if known.get(key) == etag and _location_matches(folder, src):
                result.unchanged += 1
                continue
            try:
                commit = src if full else read_commit(store, prefix or "")
            except PublicationError as exc:
                result.problems.append(f"{key}:{exc.detail}")
                continue
            if commit is None:
                result.problems.append(f"{key}:release_not_committed")  # girdi var, işaret yok: görünmez
                continue
            if not full and (commit["dataset_id"], commit["version"], commit["operation_id"]) != (ds, ver, src.get("operation_id")):
                result.problems.append(f"{key}:entry_does_not_match_commit")
                continue
            try:
                mirror_release(store, data_root, prefix, commit, provider=provider, bucket=bucket, namespace=namespace)
            except (PublicationError, KeyError) as exc:
                result.problems.append(f"{key}:{getattr(exc, 'detail', None) or type(exc).__name__}")
                seen.pop(key, None)
                continue
            result.mirrored.append(f"{ds}@{ver}")
        if full:
            # Tam kurulumdan sonra artımlı eşitleme yeniden indirme yapmasın: katalog girdilerinin ETag'leri kaydedilir.
            for info in store.list(layout.CATALOG_COMMITS):
                ident = _identity_from_entry_key(info.key)
                if ident and _location_valid(mirror_folder(data_root, *ident)):
                    seen[info.key] = info.etag
    except StorageError as exc:
        result.status = "offline" if exc.retryable else "error"
        result.error = f"{exc.code}:{exc.detail}" if exc.detail else exc.code
    folders = cloud_record_folders(data_root)
    locations = []
    for folder in folders:
        try:
            locations.append(read_json(folder / LOCATION_NAME))
        except (OSError, ValueError):
            continue
    result.release_count = len(locations)
    result.committed_bytes = sum(int(loc.get("total_bytes") or 0) for loc in locations)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_state = {**state, "provider": provider, "bucket": bucket, "namespace": namespace, "last_attempt_at": now,
                 "last_result": result.status, "last_error": result.error, "problems": result.problems[-50:],
                 "release_count": result.release_count, "committed_bytes": result.committed_bytes,
                 "committed_bytes_source": "catalog_derived_sum_of_commit_objects"}
    if result.status == "ok":
        new_state["last_success_at"] = now
        new_state["entries"] = {**known, **seen} if not full else {**known, **seen}
    write_json_atomic(cloud_dir(data_root) / SYNC_STATE_NAME, new_state)
    if rebuild and result.mirrored:
        from ..data.catalog_index import rebuild_index

        rebuild_index(data_root)
    return result
