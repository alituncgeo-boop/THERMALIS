"""ZIP arşivlerinin dizini: üye başına konum, sıkıştırma, boyut ve (açılmış) içerik SHA-256'sı. Arşiv çıkarılmaz.

Her üye bir kez akışla açılır (sınırlı bellek) ve içerik SHA-256'sı hesaplanır; isteğe bağlı olarak paketle gelen
iç manifestle karşılaştırılır. Yerel dosya başlığı okunarak verinin arşivdeki BAŞLANGIÇ baytı (``data_offset``)
kaydedilir: tek bir üye, arşivin tamamı indirilmeden ``[data_offset, data_offset + compressed_size)`` aralığından
okunur ve (deflate ise) yerelde açılıp içerik SHA-256'sıyla doğrulanır. GDAL aynı arşivi ``/vsizip/`` ile okur.

Desteklenen yöntemler: stored (0), deflate (8). Şifreli üye, desteklenmeyen yöntem veya güvensiz yol reddedilir.
"""

from __future__ import annotations

import hashlib
import struct
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

CHUNK = 8 * 1024 * 1024
METHODS = {zipfile.ZIP_STORED: "stored", zipfile.ZIP_DEFLATED: "deflate"}
_LOCAL_HEADER = struct.Struct("<IHHHHHIIIHH")
_LOCAL_SIG = 0x04034B50


class ZipIndexError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class ZipMember:
    name: str
    size: int              # açılmış boyut
    sha256: str            # açılmış içerik
    method: str            # stored | deflate
    compressed_size: int
    header_offset: int
    data_offset: int       # sıkıştırılmış verinin arşivdeki başlangıcı
    crc32: int

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ZipIndex:
    archive_bytes: int
    members: list[ZipMember] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def by_name(self) -> dict[str, ZipMember]:
        return {m.name: m for m in self.members}


def safe_member_name(name: str) -> bool:
    parts = name.split("/")
    return bool(name) and not name.startswith("/") and "\\" not in name and ".." not in parts and ":" not in name \
        and all(parts)


def _data_offset(handle, info: zipfile.ZipInfo) -> int:
    handle.seek(info.header_offset)
    raw = handle.read(_LOCAL_HEADER.size)
    if len(raw) != _LOCAL_HEADER.size:
        raise ZipIndexError("local_header_truncated", info.filename)
    fields = _LOCAL_HEADER.unpack(raw)
    if fields[0] != _LOCAL_SIG:
        raise ZipIndexError("local_header_signature_invalid", info.filename)
    name_len, extra_len = fields[9], fields[10]
    return info.header_offset + _LOCAL_HEADER.size + name_len + extra_len


def index_zip(path: Path, *, cancel: Callable[[], None] | None = None,
              progress: Callable[[int], None] | None = None) -> ZipIndex:
    """Arşivi dizinler. Dizin girdileri (klasörler) atlanır; her dosya üyesi açılıp SHA-256'sı hesaplanır."""
    path = Path(path)
    idx = ZipIndex(path.stat().st_size)
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ZipIndexError("not_a_zip", str(exc)) from None
    with zf, path.open("rb") as raw:
        names = set()
        for info in zf.infolist():
            if info.is_dir():
                continue
            if info.flag_bits & 0x1:
                raise ZipIndexError("encrypted_member", info.filename)
            if info.compress_type not in METHODS:
                raise ZipIndexError("unsupported_compression", f"{info.filename}:{info.compress_type}")
            if not safe_member_name(info.filename):
                raise ZipIndexError("unsafe_member_name", info.filename)
            if info.filename.casefold() in names:
                raise ZipIndexError("duplicate_member_name", info.filename)
            names.add(info.filename.casefold())
            digest, size = hashlib.sha256(), 0
            with zf.open(info) as stream:  # zipfile CRC-32'yi de denetler
                for block in iter(lambda: stream.read(CHUNK), b""):
                    if cancel is not None:
                        cancel()
                    digest.update(block)
                    size += len(block)
            if progress is not None:
                progress(info.compress_size)
            idx.members.append(ZipMember(info.filename, size, digest.hexdigest(), METHODS[info.compress_type],
                                         info.compress_size, info.header_offset, _data_offset(raw, info), info.CRC))
    return idx


def read_member(path: Path, member: str) -> bytes:
    with zipfile.ZipFile(path) as zf:
        return zf.read(member)


def vsizip(container: str, member: str) -> str:
    """GDAL yolu: arşiv içindeki üye/klasör. ``container`` yerel yol veya ``/vsis3/...`` olabilir."""
    return f"/vsizip/{{{container}}}/{member}" if container.startswith("/vsi") else f"/vsizip/{container}/{member}"
