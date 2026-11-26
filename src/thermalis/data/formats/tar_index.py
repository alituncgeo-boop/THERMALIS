"""Sıkıştırılmamış TAR arşivlerinin tek geçişli, sınırlı bellekli dizini.

Arşiv baştan sona bir kez akış hâlinde okunur; aynı geçişte:

- arşivin tamamının SHA-256'sı (manifestle karşılaştırılır),
- her üyenin veri başlangıç konumu (bayt), boyutu ve SHA-256'sı

hesaplanır. Bellek kullanımı parça boyutuyla sınırlıdır; arşiv çıkarılmaz, kopyalanmaz.

Üye konumu sayesinde arşiv içindeki bir dosya (ör. COG) yerelde ``/vsisubfile/<konum>_<boyut>,<arşiv>``,
bulutta ``/vsisubfile/<konum>_<boyut>,/vsis3/<kova>/<anahtar>`` ile aralıklı okunur.

Desteklenen başlıklar: POSIX ustar, GNU (uzun ad ``L``, base-256 boyut), PAX (``x`` yerel, ``g`` genel).
Sıkıştırılmış TAR (gzip/bzip2/xz/zstd) aralıklı okunamaz: :class:`TarIndexError` ``compressed_tar_not_range_readable``.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import BinaryIO, Callable

BLOCK = 512
CHUNK = 8 * 1024 * 1024
_COMPRESSED_MAGIC = {b"\x1f\x8b": "gzip", b"BZh": "bzip2", b"\xfd7zXZ\x00": "xz", b"\x28\xb5\x2f\xfd": "zstd"}
TYPE_NAMES = {b"0": "file", b"\0": "file", b"7": "file", b"1": "hardlink", b"2": "symlink", b"3": "chardev", b"4": "blockdev",
              b"5": "directory", b"6": "fifo"}


class TarIndexError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


@dataclass(frozen=True)
class TarMember:
    name: str
    offset: int          # verinin arşivdeki başlangıç baytı
    size: int
    sha256: str | None   # yalnız düzenli dosyalar için
    type: str            # file | directory | symlink | hardlink | ...
    mtime: int | None = None
    safe_name: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class TarIndex:
    archive_sha256: str
    archive_size: int
    members: list[TarMember] = field(default_factory=list)
    format: str = "ustar"
    warnings: list[str] = field(default_factory=list)

    def files(self) -> list[TarMember]:
        return [m for m in self.members if m.type == "file"]

    def member(self, name: str) -> TarMember:
        for m in self.members:
            if m.name == name:
                return m
        raise KeyError(name)

    def as_dict(self) -> dict:
        return {"archive_sha256": self.archive_sha256, "archive_size": self.archive_size, "format": self.format,
                "warnings": list(self.warnings), "members": [m.as_dict() for m in self.members]}


class _HashingReader:
    """Okunan her baytı arşiv özetine ekler ve konumu izler."""

    def __init__(self, stream: BinaryIO, cancel: Callable[[], None] | None) -> None:
        self.stream, self.cancel = stream, cancel
        self.digest = hashlib.sha256()
        self.pos = 0

    def read(self, n: int) -> bytes:
        if self.cancel is not None:
            self.cancel()
        data = self.stream.read(n)
        self.digest.update(data)
        self.pos += len(data)
        return data

    def read_exact(self, n: int) -> bytes:
        parts, left = [], n
        while left:
            block = self.read(min(left, CHUNK))
            if not block:
                raise TarIndexError("truncated_archive", f"{n - left}/{n} B at {self.pos}")
            parts.append(block)
            left -= len(block)
        return b"".join(parts)


def _number(field_bytes: bytes) -> int:
    if field_bytes and field_bytes[0] & 0x80:  # GNU base-256 (büyük boyutlar)
        value = int.from_bytes(field_bytes, "big") & ~(0xFF << (8 * (len(field_bytes) - 1)))
        return value | ((field_bytes[0] & 0x7F) << (8 * (len(field_bytes) - 1)))
    text = field_bytes.replace(b"\0", b" ").strip()
    if not text:
        return 0
    try:
        return int(text, 8)
    except ValueError:
        raise TarIndexError("invalid_header_number", repr(field_bytes[:12])) from None


def _checksum_ok(header: bytes) -> bool:
    try:
        declared = _number(header[148:156])
    except TarIndexError:
        return False
    unsigned = sum(header[:148]) + 8 * 32 + sum(header[156:])
    return declared == unsigned


def _text(raw: bytes) -> str:
    raw = raw.split(b"\0", 1)[0]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _pax(data: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    pos = 0
    while pos < len(data):
        space = data.index(b" ", pos)
        length = int(data[pos:space])
        record = data[space + 1: pos + length - 1]  # sondaki "\n" hariç
        key, _, value = record.partition(b"=")
        out[key.decode("utf-8")] = value.decode("utf-8", errors="replace")
        pos += length
    return out


def safe_member_name(name: str) -> bool:
    parts = name.replace("\\", "/").split("/")
    return bool(name) and not name.startswith("/") and ".." not in parts and ":" not in parts[0] and "\\" not in name


def detect_compression(head: bytes) -> str | None:
    for magic, kind in _COMPRESSED_MAGIC.items():
        if head.startswith(magic):
            return kind
    return None


def index_tar(stream: BinaryIO, *, cancel: Callable[[], None] | None = None,
              progress: Callable[[int], None] | None = None) -> TarIndex:
    """Arşivi tek geçişte dizinler. ``cancel()`` iptal edilmişse istisna atmalıdır; ``progress(n)`` okunan bayt artışı."""
    reader = _HashingReader(stream, cancel)
    members: list[TarMember] = []
    warnings: list[str] = []
    fmt = "ustar"
    pending_name: str | None = None
    pending_pax: dict[str, str] = {}
    zero_blocks = 0
    first = True
    while True:
        header = reader.read(BLOCK)
        if progress is not None and header:
            progress(len(header))
        if not header:
            break
        if first:
            kind = detect_compression(header)
            if kind:
                raise TarIndexError("compressed_tar_not_range_readable", kind)
        if len(header) < BLOCK:
            raise TarIndexError("truncated_archive", f"partial header at {reader.pos - len(header)}")
        if header == b"\0" * BLOCK:
            zero_blocks += 1
            if zero_blocks >= 2:
                break
            continue
        if zero_blocks:
            warnings.append(f"single_zero_block_at:{reader.pos - 2 * BLOCK}")
            zero_blocks = 0
        if not _checksum_ok(header):
            raise TarIndexError("not_a_tar" if first else "header_checksum_mismatch", f"at {reader.pos - BLOCK}")
        first = False
        typeflag = header[156:157]
        size = _number(header[124:136])
        offset = reader.pos
        padded = -(-size // BLOCK) * BLOCK
        if typeflag in (b"x", b"g", b"L", b"K"):
            data = reader.read_exact(padded)[:size] if padded else b""
            if progress is not None and padded:
                progress(padded)
            if typeflag == b"x":
                pending_pax = _pax(data)
                fmt = "pax"
            elif typeflag == b"g":
                fmt = "pax"
                warnings.append("pax_global_header_present")
            elif typeflag == b"L":
                pending_name = _text(data)
                fmt = "gnu"
            continue
        name = _text(header[0:100])
        magic = header[257:263]
        if magic.startswith(b"ustar"):
            prefix = _text(header[345:500])
            if prefix:
                name = f"{prefix}/{name}"
        if pending_name is not None:
            name = pending_name
        if "path" in pending_pax:
            name = pending_pax["path"]
        if "size" in pending_pax:
            size = int(pending_pax["size"])
            padded = -(-size // BLOCK) * BLOCK
        pending_name, pending_pax = None, {}
        mtime = _number(header[136:148])
        kind = TYPE_NAMES.get(typeflag, f"type_{typeflag.decode('latin-1')!r}")
        digest = hashlib.sha256() if kind == "file" else None
        left = size
        while left:
            block = reader.read(min(left, CHUNK))
            if not block:
                raise TarIndexError("truncated_archive", f"member {name}")
            if digest is not None:
                digest.update(block)
            left -= len(block)
            if progress is not None:
                progress(len(block))
        if padded - size:
            pad = reader.read_exact(padded - size)
            if progress is not None:
                progress(len(pad))
        members.append(TarMember(name=name, offset=offset, size=size, sha256=digest.hexdigest() if digest else None,
                                 type=kind, mtime=mtime, safe_name=safe_member_name(name)))
    # Arşiv sonu dolgusu (kayıt bloğu doldurma) da arşiv özetine dahildir.
    while True:
        rest = reader.read(CHUNK)
        if not rest:
            break
        if progress is not None:
            progress(len(rest))
        if rest.strip(b"\0"):
            warnings.append("non_zero_bytes_after_end_of_archive")
    names = [m.name for m in members]
    if len(names) != len(set(names)):
        warnings.append("duplicate_member_names")
    return TarIndex(reader.digest.hexdigest(), reader.pos, members, fmt, warnings)


def vsisubfile(member: TarMember | dict, container_path: str) -> str:
    """GDAL yolu: arşiv içindeki üyeyi çıkarmadan okur (yerel yol veya ``/vsis3/...``)."""
    if isinstance(member, dict):  # dizin kaydı (``size``) veya koleksiyon varlığı (``bytes``)
        offset, size = member["offset"], member["size"] if "size" in member else member["bytes"]
    else:
        offset, size = member.offset, member.size
    return f"/vsisubfile/{int(offset)}_{int(size)},{container_path}"
