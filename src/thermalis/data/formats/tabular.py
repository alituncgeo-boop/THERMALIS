"""Genel bilimsel tablo (CSV) profili — akış hâlinde, sınırlı bellek, değer DÖNÜŞTÜRMEDEN.

Kaydedilenler yalnız dosyada gözlenen/beyan edilen özelliklerdir:
- kodlama (katı UTF-8; BOM), satır sonu, ayırıcı (``,`` ``;`` sekme), fiziksel satır sayısı
- boş satırla ayrılmış BÖLÜMLER (ör. konum metadata bloğu + saatlik tablo): her bölümün başlığı ve kayıt sayısı,
  hatalı genişlikte satırlar
- sütun başına: başlıkta beyan edilen birim (``ad (birim)`` biçimi; yoksa ``None``), sayısal/boş/metin sayıları,
  sayısal en küçük/en büyük (betimleyici), ISO 8601 zaman sütunu ise ilk/son etiket, ayırt edici değer sayısı ve
  yinelenen etiket sayısı (zaman bölgesi EKLENMEZ: etiketler dosyadaki gibi kalır)

Birim çevirisi, eksik değer doldurma, saat dilimi yeniden yorumlama YAPILMAZ.
"""

from __future__ import annotations

import codecs
import csv
import io
import re
from typing import Any, BinaryIO

UNIT_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<unit>[^()]*)\)\s*$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$")
NUM_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
DELIMITERS = (",", ";", "\t")
MAX_DISTINCT_TRACK = 5_000_000


def _column_profile() -> dict[str, Any]:
    return {"numeric": 0, "empty": 0, "text": 0, "min": None, "max": None, "iso": 0, "first": None, "last": None,
            "_seen": set(), "duplicates": 0}


def profile_csv(stream: BinaryIO, *, max_bytes: int | None = None) -> dict[str, Any]:
    """CSV akışının profili. ``stream`` ikili okunur; katı UTF-8 çözümlenir (hata → ``encoding_error``)."""
    raw_head = stream.read(4)
    bom = raw_head.startswith(codecs.BOM_UTF8)
    rest = raw_head[3:] if bom else raw_head
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    line_endings = {"CRLF": 0, "LF": 0}

    def lines():
        buffer = ""
        nonlocal rest
        chunk = rest
        while True:
            data = decoder.decode(chunk, final=not chunk)
            buffer += data
            while True:
                i = buffer.find("\n")
                if i < 0:
                    break
                line, buffer = buffer[: i + 1], buffer[i + 1:]
                if line.endswith("\r\n"):
                    line_endings["CRLF"] += 1
                else:
                    line_endings["LF"] += 1
                yield line.rstrip("\r\n"), True
            if not chunk:
                if buffer:
                    yield buffer.rstrip("\r"), False
                return
            chunk = stream.read(1 << 20)

    out: dict[str, Any] = {"encoding": "utf-8", "bom": bom, "physical_lines": 0, "sections": [], "delimiter": None,
                           "trailing_newline": None, "encoding_error": None}
    section: dict[str, Any] | None = None
    source = lines()
    try:
        first = next(source, None)
    except UnicodeDecodeError as exc:
        out["encoding_error"] = f"utf-8 decode error at byte {exc.start}"
        first = None
    if first is None:
        out["line_endings"] = line_endings
        return out
    delim = max(DELIMITERS, key=first[0].count)
    out["delimiter"] = delim

    def raw_lines():
        for item in ([first] if first else []):
            out["physical_lines"] += 1
            out["trailing_newline"] = item[1]
            yield item[0]
        for line, terminated in source:
            out["physical_lines"] += 1
            out["trailing_newline"] = terminated
            yield line

    try:
        for row in csv.reader(raw_lines(), delimiter=delim):
            if not row or not any(cell.strip() for cell in row):  # boş satır: bölüm sınırı
                if section is not None:
                    out["sections"].append(_finish(section))
                    section = None
                continue
            if section is None:
                section = {"header_line": out["physical_lines"], "header": row, "records": 0, "wrong_width": 0,
                           "columns": [_column_profile() for _ in row]}
                continue
            section["records"] += 1
            if len(row) != len(section["header"]):
                section["wrong_width"] += 1
                continue
            for value, col in zip(row, section["columns"]):
                v = value.strip()
                if not v:
                    col["empty"] += 1
                    continue
                c0 = v[0]
                if (c0.isdigit() or c0 in "+-.") and NUM_RE.match(v):
                    col["numeric"] += 1
                    x = float(v)
                    if col["min"] is None or x < col["min"]:
                        col["min"] = x
                    if col["max"] is None or x > col["max"]:
                        col["max"] = x
                else:
                    col["text"] += 1
                    if ISO_RE.match(v):
                        col["iso"] += 1
                        col["first"] = col["first"] or v
                        col["last"] = v
                        if col["_seen"] is not None:
                            if v in col["_seen"]:
                                col["duplicates"] += 1
                            else:
                                col["_seen"].add(v)
                                if len(col["_seen"]) > MAX_DISTINCT_TRACK:
                                    col["_seen"] = None
    except UnicodeDecodeError as exc:
        out["encoding_error"] = f"utf-8 decode error at byte {exc.start}"
    if section is not None:
        out["sections"].append(_finish(section))
    out["line_endings"] = line_endings
    return out


def _unit_like(token: str) -> bool:
    """Başlıktaki parantez içi ifade birim gibi mi (kısa, boşluksuz, virgülsüz: °C, %, km/h, W/m², hPa)?"""
    token = token.strip()
    return 0 < len(token) <= 10 and not any(ch.isspace() for ch in token) and "," not in token


def _finish(section: dict[str, Any]) -> dict[str, Any]:
    cols = []
    for name, c in zip(section["header"], section["columns"]):
        m = UNIT_RE.match(name)
        if m and not _unit_like(m.group("unit")):
            m = None  # parantez içi bir etiket (ör. "(Aile, akraba vb.)") birim sayılmaz
        is_time = c["iso"] > 0 and c["iso"] == (section["records"] - section["wrong_width"] - c["empty"])
        entry = {"name": name, "base_name": m.group("name") if m else name, "header_declared_unit": m.group("unit") if m else None,
                 "numeric_count": c["numeric"], "empty_count": c["empty"], "text_count": c["text"],
                 "numeric_min": c["min"], "numeric_max": c["max"], "temporal": None}
        if is_time:
            entry["temporal"] = {"first_label": c["first"], "last_label": c["last"],
                                 "distinct_labels": None if c["_seen"] is None else len(c["_seen"]),
                                 "repeated_labels": None if c["_seen"] is None else c["duplicates"],
                                 "zone": "as_labelled_in_source (no offset applied)"}
        cols.append(entry)
    return {"header_line": section["header_line"], "records": section["records"], "wrong_width_rows": section["wrong_width"],
            "columns": cols}


def profile_bytes(data: bytes) -> dict[str, Any]:
    return profile_csv(io.BytesIO(data))
