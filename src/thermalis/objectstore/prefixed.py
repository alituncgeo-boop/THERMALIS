"""Önek (namespace) sarmalayıcısı: tüm anahtarları sabit bir önek altına alır.

Kullanım: canlı testlerin üretim kayıtlarından yalıtılması (``e2e-tests/<çalıştırma>/``) ve ayarlardaki
isteğe bağlı ``cloud.namespace``. Sarmalanan depo koşullu yazım semantiğini aynen korur.
"""

from __future__ import annotations

from typing import BinaryIO, Iterator

from .base import ObjectInfo, ObjectStore
from .keys import validate_prefix


class PrefixedStore(ObjectStore):
    def __init__(self, inner: ObjectStore, prefix: str) -> None:
        self.inner = inner
        self.prefix = validate_prefix(prefix)

    @property
    def capabilities(self):
        return self.inner.capabilities

    def _k(self, key: str) -> str:
        return self.prefix + key

    def _strip(self, info: ObjectInfo | None) -> ObjectInfo | None:
        if info is None:
            return None
        return ObjectInfo(info.key[len(self.prefix):], info.size, info.etag, info.last_modified, info.sha256, info.operation_id,
                          info.metadata)

    def head(self, key):
        return self._strip(self.inner.head(self._k(key)))

    def list(self, prefix: str = "", *, page_size: int = 1000) -> Iterator[ObjectInfo]:
        for info in self.inner.list(self.prefix + prefix, page_size=page_size):
            yield self._strip(info)

    def open_read(self, key) -> BinaryIO:
        return self.inner.open_read(self._k(key))

    def read_range(self, key, start, end):
        return self.inner.read_range(self._k(key), start, end)

    def put_bytes(self, key, data, **kw):
        return self._strip(self.inner.put_bytes(self._k(key), data, **kw))

    def upload_file(self, key, path, **kw):
        return self._strip(self.inner.upload_file(self._k(key), path, **kw))

    def gdal_path(self, key):
        return self.inner.gdal_path(self._k(key))

    def local_path(self, key):
        return self.inner.local_path(self._k(key))


def namespaced(store: ObjectStore, namespace: str) -> ObjectStore:
    return PrefixedStore(store, namespace) if namespace else store
