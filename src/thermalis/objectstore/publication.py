"""Değişmez sürüm yayın protokolü (bulut). Bu fazda üretim yayını AÇIK DEĞİLDİR.

Düzen (``releases/`` altında, yerel ``datasets/`` düzeninin karşılığı):

    releases/<SEG>/<SEG>/<sürüm>/payload/<göreli yol>        yalnız-oluştur
    releases/<SEG>/<SEG>/<sürüm>/records/version.json        yalnız-oluştur (bilimsel kayıt)
    releases/<SEG>/<SEG>/<sürüm>/records/quality.json        yalnız-oluştur
    releases/<SEG>/<SEG>/<sürüm>/records/integrity.json      yalnız-oluştur
    releases/<SEG>/<SEG>/<sürüm>/_COMMIT.json                yalnız-oluştur, EN SON yazılır
    state/datasets/<SEG>/<SEG>/releases.json                 türetilmiş işaretçi (If-Match ile güncellenir)

**Yetkili yayın kaydı ``_COMMIT.json``'dır.** Bir sürüm yalnız bu işaret varsa ve geçerliyse görünür;
önek altında nesnelerin bulunması tek başına hiçbir şey ifade etmez (yarım yayın görünmez).
İşaret, tüm nesneler yazılıp **her biri baştan sona okunarak SHA-256 ile bağımsız doğrulandıktan sonra**
yazılır ve her nesnenin anahtarını, boyutunu ve SHA-256'sını listeler.

Kurtarma: yayın aynı ``operation_id`` ile yeniden çalıştırılır. Yalnız-oluştur yazımları aynı işlemin aynı
içeriğini başarı sayar, kalan nesneler yazılır, doğrulama yapılır, işaret yazılır. Farklı işlem kimliğiyle
aynı sürüme yazma girişimi çakışmadır (:class:`AlreadyExists`); hiçbir nesnenin üzerine yazılmaz, silme yoktur.

Yerel katalog dizini buluttan şöyle yeniden kurulur: :func:`committed_releases` ile yalnız işaretli sürümler
listelenir, her birinin ``records/*.json`` dosyaları (işaretteki SHA-256 ile doğrulanarak) okunur ve bugünkü
``catalog_index`` kurulumuna verilir. ``state/`` altındaki işaretçiler yetkili değildir; işaretlerden yeniden üretilir.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .base import CancelToken, ObjectStore, is_sha256_hex
from .errors import AlreadyExists, NotFound, PreconditionFailed, PublicationError, PublicationNotAuthorized
from .keys import validate_key

COMMIT_NAME = "_COMMIT.json"
COMMIT_SCHEMA = "thermalis.release-commit/1"
POINTER_SCHEMA = "thermalis.release-pointer/1"
RELEASES_ROOT = "releases/"
REQUIRED_RECORDS = ("version.json", "quality.json", "integrity.json")


@dataclass(frozen=True)
class PublicationPolicy:
    """Bulut yayın kapısı. Varsayılan: kapalı. Geliştirme kovası yalnız sentetik/açık lisanslı veri alır."""

    allow_cloud_publication: bool = False
    allowed_access_classes: tuple[str, ...] = ("SYNTHETIC",)


@dataclass(frozen=True)
class ReleaseFile:
    relpath: str
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class ReleaseState:
    status: str  # ABSENT | INCOMPLETE | COMMITTED | CORRUPT
    prefix: str
    operation_ids: tuple[str, ...] = ()
    object_count: int = 0
    problems: tuple[str, ...] = field(default_factory=tuple)


def new_operation_id(kind: str = "pub") -> str:
    return f"{kind}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:12]}"


def release_prefix(dataset_id: str, version: str) -> str:
    from ..data.catalog import dataset_segments
    from ..data.transfer_package import VERSION_PATTERN

    if not VERSION_PATTERN.match(version):
        raise PublicationError("invalid_version")
    return validate_key("/".join([RELEASES_ROOT.rstrip("/"), *dataset_segments(dataset_id), version])) + "/"


def pointer_key(dataset_id: str) -> str:
    from ..data.catalog import dataset_segments

    return validate_key("/".join(["state", "datasets", *dataset_segments(dataset_id), "releases.json"]))


def _canonical(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode("utf-8")


def _validate_commit(prefix: str, data: object) -> dict:
    if not isinstance(data, dict) or data.get("schema") != COMMIT_SCHEMA:
        raise PublicationError("commit_schema_invalid", key=prefix + COMMIT_NAME)
    for name in ("dataset_id", "version", "operation_id", "committed_at", "objects", "access_class"):
        if name not in data:
            raise PublicationError(f"commit_field_missing:{name}", key=prefix + COMMIT_NAME)
    for obj in data["objects"]:
        if not isinstance(obj, dict) or not str(obj.get("key", "")).startswith(prefix) or not is_sha256_hex(obj.get("sha256")) \
                or not isinstance(obj.get("size"), int):
            raise PublicationError("commit_object_entry_invalid", key=prefix + COMMIT_NAME)
    records = {o["key"][len(prefix):] for o in data["objects"]}
    if any(f"records/{r}" not in records for r in REQUIRED_RECORDS):
        raise PublicationError("commit_missing_required_records", key=prefix + COMMIT_NAME)
    return data


def read_commit(store: ObjectStore, prefix: str) -> dict | None:
    try:
        with store.open_read(prefix + COMMIT_NAME) as handle:
            raw = handle.read()
    except NotFound:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PublicationError("commit_not_json", key=prefix + COMMIT_NAME) from None
    return _validate_commit(prefix, data)


def publish_release(store: ObjectStore, *, dataset_id: str, version: str, access_class: str, files: list[ReleaseFile],
                    records: dict[str, bytes], operation_id: str, policy: PublicationPolicy,
                    cancel: CancelToken | None = None) -> dict:
    """Sürümü yayımlar veya aynı ``operation_id`` ile yarım kalmış yayını tamamlar. Yetkili kaydı döndürür."""
    if not policy.allow_cloud_publication:
        raise PublicationNotAuthorized("cloud_publication_disabled")
    if access_class not in policy.allowed_access_classes:
        raise PublicationNotAuthorized(f"access_class_not_allowed:{access_class}")
    missing = [r for r in REQUIRED_RECORDS if r not in records]
    if missing:
        raise PublicationError(f"required_records_missing:{','.join(missing)}")
    prefix = release_prefix(dataset_id, version)
    existing = read_commit(store, prefix)
    if existing is not None:
        if existing["operation_id"] == operation_id:
            return existing  # tekrar: zaten tamam
        raise AlreadyExists("release_already_committed_by_other_operation", key=prefix + COMMIT_NAME)

    planned: list[dict] = []
    for f in sorted(files, key=lambda f: f.relpath):
        key = validate_key(prefix + "payload/" + validate_key(f.relpath))
        if cancel is not None:
            cancel.raise_if_cancelled(key)
        store.upload_file(key, f.path, expected_sha256=f.sha256, operation_id=operation_id, metadata={"thermalis-part": "payload"},
                          cancel=cancel)
        planned.append({"key": key, "sha256": f.sha256, "size": f.size})
    import hashlib

    for name in sorted(records):
        key = validate_key(prefix + "records/" + validate_key(name))
        data = records[name]
        store.put_bytes(key, data, create_only=True, operation_id=operation_id, metadata={"thermalis-part": "record"}, cancel=cancel)
        planned.append({"key": key, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})

    # Bağımsız doğrulama: her nesne baştan sona okunur. Biri bile tutmazsa işaret yazılmaz.
    failures = []
    for obj in planned:
        result = store.verify_object(obj["key"], obj["sha256"], expected_size=obj["size"], cancel=cancel)
        if not result.ok:
            failures.append(f"{obj['key']}:{result.problem}")
    if failures:
        raise PublicationError("verification_failed_release_not_committed:" + ";".join(failures[:10]), key=prefix)

    commit = {"schema": COMMIT_SCHEMA, "dataset_id": dataset_id, "version": version, "access_class": access_class,
              "operation_id": operation_id, "committed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "verification": "full_sha256_read_back", "objects": planned}
    try:
        store.put_bytes(prefix + COMMIT_NAME, _canonical(commit), create_only=True, operation_id=operation_id,
                        metadata={"thermalis-part": "commit"}, cancel=cancel)
    except AlreadyExists:
        current = read_commit(store, prefix)
        if current is not None and current["operation_id"] == operation_id:
            return current
        raise
    return _validate_commit(prefix, commit)


def inspect_release(store: ObjectStore, prefix: str, *, full: bool = False) -> ReleaseState:
    """Bir sürüm önekinin durumu. Yalnız işaret + işaretteki tüm nesneler tutarlıysa ``COMMITTED``."""
    objects = list(store.list(prefix))
    if not objects:
        return ReleaseState("ABSENT", prefix)
    try:
        commit = read_commit(store, prefix)
    except PublicationError as exc:
        return ReleaseState("CORRUPT", prefix, object_count=len(objects), problems=(exc.detail,))
    if commit is None:
        ops = sorted({i.operation_id for i in (store.head(o.key) for o in objects) if i is not None and i.operation_id})
        return ReleaseState("INCOMPLETE", prefix, tuple(ops), len(objects))
    problems = []
    for obj in commit["objects"]:
        check = store.verify_object(obj["key"], obj["sha256"], expected_size=obj["size"]) if full else \
            store.check_metadata(obj["key"], expected_size=obj["size"], expected_sha256=obj["sha256"])
        if not check.ok:
            problems.append(f"{obj['key']}:{check.problem}")
    return ReleaseState("CORRUPT" if problems else "COMMITTED", prefix, (commit["operation_id"],), len(objects), tuple(problems))


def committed_releases(store: ObjectStore, root: str = RELEASES_ROOT) -> Iterator[tuple[str, dict | None, str | None]]:
    """(önek, işaret, sorun) — yalnız işaret dosyası olan sürümler. Geçersiz işaret ``sorun`` ile döner, sürüm sayılmaz."""
    for info in store.list(root):
        if info.key.rsplit("/", 1)[-1] != COMMIT_NAME:
            continue
        prefix = info.key[: -len(COMMIT_NAME)]
        try:
            yield prefix, read_commit(store, prefix), None
        except PublicationError as exc:
            yield prefix, None, exc.detail


def reconcile_dataset_pointer(store: ObjectStore, dataset_id: str, *, operation_id: str, attempts: int = 5) -> dict:
    """``state/.../releases.json`` işaretçisini işaretlerden yeniden üretir (If-Match ile; yarışta tekrar dener).

    Yayından sonra işaretçi güncellenmeden kesilen işlemler bu çağrıyla uzlaştırılır. İşaretçi yetkili değildir.
    """
    from ..data.catalog_index import version_sort_key

    prefix = release_prefix(dataset_id, "0.0.0")[: -len("0.0.0/")]
    key = pointer_key(dataset_id)
    for _ in range(attempts):
        versions = sorted({c["version"] for _, c, problem in committed_releases(store, prefix) if c and not problem},
                          key=version_sort_key)
        body = _canonical({"schema": POINTER_SCHEMA, "dataset_id": dataset_id, "versions": versions,
                           "latest": versions[-1] if versions else None, "derived_from": "release commit markers",
                           "authoritative": False})
        current = store.head(key)
        try:
            if current is None:
                store.put_bytes(key, body, create_only=True, operation_id=operation_id)
            else:
                with store.open_read(key) as handle:
                    if handle.read() == body:
                        return json.loads(body)
                store.put_bytes(key, body, if_match=current.etag, operation_id=operation_id)
            return json.loads(body)
        except (AlreadyExists, PreconditionFailed):
            continue  # başka yazar araya girdi: yeniden oku, yeniden üret
    raise PublicationError("pointer_update_contended", key=key)
