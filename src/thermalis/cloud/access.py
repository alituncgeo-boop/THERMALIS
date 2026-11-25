"""Bulut sürümüne yerel erişim: doğrulanmış malzemeleştirme (materialization).

Analiz bileşenleri bu bilgisayarda çalışır. Bulut sürümünde yerel dosya yolu yoktur; bileşen bir varlığı
(ör. File Geodatabase klasörü) okumak istediğinde varlığın nesneleri sınırlı önbelleğe indirilir, her biri
commit işaretindeki SHA-256 ile doğrulanır ve klasör yapısıyla yerel bir yol olarak verilir.

- Yalnız istenen varlık indirilir (sürümün tamamı değil).
- Önbellek sınırı (varsayılan 20 GB) bir veri seti boyut sınırı DEĞİLDİR; yalnız aynı anda yerelde tutulabilecek
  çalışma kümesinin sınırıdır. Tek bir varlık sınırdan büyükse malzemeleştirilmez (``cache_capacity_exceeded``);
  bu durumda büyük bulut-yerel biçimler için aralıklı okuma (``/vsis3/``) kullanılmalıdır.
- İndirmeden önce disk boşluğu denetlenir (``insufficient_disk_space``).
- Yerel sürümde hiçbir şey kopyalanmaz: yerel yol olduğu gibi verilir.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Callable

from ..objectstore.base import ObjectStore
from ..objectstore.cache import CacheFull, CacheItem, ObjectCache
from ..objectstore.errors import StorageError

DISK_MARGIN = 512 * 1024 * 1024


class CacheCapacityExceeded(StorageError):
    code = "cache_capacity_exceeded"


class InsufficientDiskSpace(StorageError):
    code = "insufficient_disk_space"


def asset_items(commit: dict[str, Any], prefix: str, asset_path: str) -> list[CacheItem]:
    """Bir varlığın (dosya veya kapsayıcı klasör) commit işaretindeki nesneleri."""
    base = prefix + "payload/"
    target = base + asset_path
    items = [CacheItem(o["key"], o["key"][len(base):], o["sha256"], o["size"]) for o in commit["objects"]
             if o["key"] == target or o["key"].startswith(target + "/")]
    return sorted(items, key=lambda i: i.relpath)


def materialize_asset(data_root: Path, store: ObjectStore | None, cache: ObjectCache | None, layer_dataset_id: str,
                      version: str | None = None, *, disk_free: Callable[[Path], int] | None = None) -> dict[str, Any]:
    from ..data.catalog_index import storage_location
    from ..data.contract import _layer, _physical

    layer = _layer(data_root, layer_dataset_id, version)
    asset, folder = _physical(data_root, layer)
    if asset is None:
        raise StorageError("asset_not_recorded", key=layer_dataset_id)
    loc = storage_location(data_root, layer["dataset_id"], layer["version"]) or {"backend": "local"}
    if loc["backend"] == "local":
        return {"backend": "local", "path": str(folder / "payload" / asset["path"]), "materialized": False,
                "dataset_version": layer["dataset_version"]}
    if store is None or cache is None:
        raise StorageError("cloud_store_not_available", key=layer_dataset_id)
    commit = json.loads((folder / "_COMMIT.json").read_text(encoding="utf-8"))
    items = asset_items(commit, loc["prefix"], asset["path"])
    if not items:
        raise StorageError("asset_not_in_commit", key=asset["path"])
    total = sum(i.size for i in items)
    if total > cache.max_bytes or any(i.size > cache.max_bytes for i in items):
        raise CacheCapacityExceeded(f"asset={total} cache_max={cache.max_bytes}", key=asset["path"])
    missing = sum(i.size for i in items if not cache.contains(i.sha256, i.size))
    free = (disk_free or (lambda p: shutil.disk_usage(p).free))(cache.root)
    if free < missing + DISK_MARGIN:
        raise InsufficientDiskSpace(f"needed={missing + DISK_MARGIN} free={free}", key=asset["path"])
    name = "m-" + hashlib.sha256(f"{layer['dataset_id']}|{layer['version']}|{asset['path']}".encode("utf-8")).hexdigest()[:16]
    try:
        with cache.materialize(store, name, items) as root:
            path = root.joinpath(*asset["path"].split("/"))
    except CacheFull as exc:
        raise CacheCapacityExceeded(exc.detail, key=asset["path"]) from None
    return {"backend": "r2", "path": str(path), "materialized": True, "bytes": total, "files": len(items),
            "dataset_version": layer["dataset_version"], "verification": "sha256_per_object_against_commit",
            "lease": "released_after_response",
            "note": "Önbellekteki kopya salt okunurdur ve önbellek dolarsa tahliye edilebilir; kullanmadan hemen önce yeniden isteyin."}
