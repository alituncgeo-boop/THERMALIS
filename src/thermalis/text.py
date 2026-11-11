"""Türkçe karakterlere duyarlı metin yardımcıları.

Python'un yerleşik büyük/küçük harf dönüşümü Türkçe için yanlıştır:
``"i".upper() == "I"`` (doğrusu "İ") ve ``"İ".lower()`` birleşik nokta işareti üretir.
Eşleştirme, arama ve kimlik üretimi her zaman bu modül üzerinden yapılır.
"""

from __future__ import annotations

import re
import unicodedata

_TR_LOWER = str.maketrans({"I": "ı", "İ": "i"})
_TR_UPPER = str.maketrans({"i": "İ", "ı": "I"})
_ASCII_FOLD = str.maketrans(
    {
        "ç": "c", "Ç": "c", "ğ": "g", "Ğ": "g", "ı": "i", "I": "i", "İ": "i",
        "ö": "o", "Ö": "o", "ş": "s", "Ş": "s", "ü": "u", "Ü": "u",
        "â": "a", "Â": "a", "î": "i", "Î": "i", "û": "u", "Û": "u",
    }
)


def tr_lower(value: str) -> str:
    """Türkçe kurallarıyla küçük harfe çevirir (I→ı, İ→i)."""
    return value.translate(_TR_LOWER).lower()


def tr_upper(value: str) -> str:
    """Türkçe kurallarıyla büyük harfe çevirir (i→İ, ı→I)."""
    return value.translate(_TR_UPPER).upper()


def match_key(value: str) -> str:
    """Arama/eşleştirme anahtarı: Türkçe harfler ASCII'ye katlanır, boşluklar sadeleşir.

    "İSTANBUL", "istanbul" ve "Istanbul" aynı anahtarı üretir.
    """
    folded = unicodedata.normalize("NFC", value).translate(_ASCII_FOLD).lower()
    return " ".join(folded.split())


def slugify(value: str, fallback: str = "item") -> str:
    """Dosya ve kimlik için güvenli kısa ad (yalnız a-z, 0-9 ve tire)."""
    slug = re.sub(r"[^a-z0-9]+", "-", match_key(value)).strip("-")
    return slug[:40].rstrip("-") or fallback
