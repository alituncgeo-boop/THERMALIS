"""Nesne anahtarı (key) doğrulaması.

Anahtar, sağlayıcıdan bağımsız göreli bir yoldur: ``/`` ayırıcılı, boş/``.``/``..`` bölümsüz.
Aynı anahtar yerel diskte (Windows dahil) ve S3/R2'de aynı nesneyi gösterebilmelidir; bu yüzden
kural en kısıtlayıcı ortama göre konur: Windows'ta geçersiz karakterler, ayrılmış aygıt adları
(``CON``, ``NUL``...) ve nokta/boşlukla biten bölümler reddedilir. Kullanıcıdan veya paketten gelen
hiçbir yol doğrulanmadan anahtara çevrilmez (yol geçişi / path traversal engeli).
"""

from __future__ import annotations

import re

from .errors import InvalidKey

MAX_KEY_BYTES = 1024  # S3/R2 sınırı
MAX_SEGMENT_CHARS = 255
_FORBIDDEN_CHARS = set('<>:"\\|?*')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_PREFIX_RE = re.compile(r"^(?:[^/]+/)*$")
INTERNAL_PREFIX = ".thermalis-"  # arka uçların iç dosyaları (geçici/metadata); listelenmez, yazılamaz


def _check_segment(segment: str, key: str) -> None:
    if segment in ("", ".", ".."):
        raise InvalidKey("empty_or_relative_segment", key=key)
    if len(segment) > MAX_SEGMENT_CHARS:
        raise InvalidKey("segment_too_long", key=key)
    if any(ord(ch) < 32 or ch == "\x7f" for ch in segment):
        raise InvalidKey("control_character", key=key)
    if any(ch in _FORBIDDEN_CHARS for ch in segment):
        raise InvalidKey("forbidden_character", key=key)
    if segment[-1] in (" ", "."):
        raise InvalidKey("trailing_dot_or_space", key=key)
    if segment.split(".", 1)[0].upper() in _RESERVED:
        raise InvalidKey("reserved_name", key=key)
    if segment.startswith(INTERNAL_PREFIX):
        raise InvalidKey("internal_name", key=key)


def validate_key(key: str) -> str:
    """Geçerli anahtarı aynen döndürür; değilse :class:`InvalidKey` yükseltir."""
    if not isinstance(key, str) or not key:
        raise InvalidKey("empty", key=str(key) if key is not None else None)
    if len(key.encode("utf-8")) > MAX_KEY_BYTES:
        raise InvalidKey("too_long", key=key[:80])
    if key.startswith("/") or key.endswith("/"):
        raise InvalidKey("leading_or_trailing_slash", key=key)
    if re.match(r"^[A-Za-z]:", key):
        raise InvalidKey("drive_letter", key=key)
    for segment in key.split("/"):
        _check_segment(segment, key)
    return key


def validate_prefix(prefix: str) -> str:
    """Liste öneki: boş ya da ``/`` ile biten geçerli bölümler."""
    if prefix == "":
        return prefix
    if not _PREFIX_RE.match(prefix):
        raise InvalidKey("prefix_must_end_with_slash", key=prefix)
    validate_key(prefix[:-1])
    return prefix


def join(*parts: str) -> str:
    return validate_key("/".join(p.strip("/") for p in parts if p))
