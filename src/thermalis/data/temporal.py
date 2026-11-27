"""Bilimsel zaman metadata modeli (vektör, raster, tablo, çok boyutlu ürünler için ortak).

Her zaman bilgisi ANLAMIYLA ayrı bir girdidir; gözlem dönemi yayın tarihiyle, pasaport geçerliliği ölçüm
tarihiyle, kayıt tarihi kaynak tarihiyle karıştırılmaz. Kaynakta tarih yoksa girdi ``status: UNKNOWN`` ile kalır;
kayıt (registration) tarihi hiçbir zaman kaynak tarihinin yerine konmaz.

Girdi biçimi::

    {"kind": "observation_period", "start": "...", "end": "...", "continuity": "continuous",
     "status": "SOURCE_DATA" | "SOURCE_DECLARED" | "INFERRED_CORROBORATED" | "UNKNOWN",
     "scope": "dataset" | "<katman>", "basis": "...", "evidence": [...]}
"""

from __future__ import annotations

from typing import Any

KINDS = ("observation_period", "acquisition_date", "acquisition_span", "source_reference_date", "demographic_reference_year",
         "administrative_geography_vintage", "survey_date", "product_generation_date", "publication_date", "validity_period")
CONTINUITY = ("continuous", "discontinuous", "selected_acquisitions", "single_date", "unknown")
STATUSES = ("SOURCE_DATA", "SOURCE_DECLARED", "INFERRED_CORROBORATED", "UNKNOWN")
# Eski beyan anlamları (``temporal_coverage.semantics``) → yeni türler (geriye uyumluluk).
LEGACY = {"observation_period": "observation_period", "source_acquisition_span": "acquisition_span",
          "reference_period": "source_reference_date", "validity_period": "validity_period", "unknown": "source_reference_date"}


def _entry(kind: str, start: Any = None, end: Any = None, *, continuity: str = "unknown", status: str | None = None,
           scope: str = "dataset", basis: str | None = None, evidence: list | None = None, note: str | None = None) -> dict[str, Any]:
    has = start is not None or end is not None
    return {"kind": kind, "start": start, "end": end, "continuity": continuity,
            "status": status or ("SOURCE_DECLARED" if has else "UNKNOWN"), "scope": scope, "basis": basis,
            "evidence": evidence or [], "note": note}


def entries(record: dict[str, Any], profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Kayıttan zaman girdileri. Açık girdiler (``source.temporal_entries``, ``layers.*.temporal``) önceliklidir."""
    src = record.get("source") or {}
    out: list[dict[str, Any]] = []
    for e in src.get("temporal_entries") or []:
        if isinstance(e, dict) and e.get("kind") in KINDS:
            out.append(_entry(e["kind"], e.get("start"), e.get("end"), continuity=e.get("continuity", "unknown"),
                              status=e.get("status"), scope=e.get("scope", "dataset"), basis=e.get("basis"),
                              evidence=e.get("evidence"), note=e.get("note")))
    for lname, layer in (record.get("layers") or {}).items():
        for e in (layer or {}).get("temporal") or []:
            if isinstance(e, dict) and e.get("kind") in KINDS:
                out.append(_entry(e["kind"], e.get("start"), e.get("end"), continuity=e.get("continuity", "unknown"),
                                  status=e.get("status"), scope=lname, basis=e.get("basis"), evidence=e.get("evidence"),
                                  note=e.get("note")))
    tc = src.get("temporal_coverage")
    if isinstance(tc, dict) and "semantics" in tc and not any(o["scope"] == "dataset" for o in out):
        kind = LEGACY.get(tc.get("semantics"), "source_reference_date")
        cont = "continuous" if kind == "observation_period" and tc.get("start") else "unknown"
        status = "SOURCE_DATA" if str(tc.get("basis") or "").startswith("data:") else None
        out.append(_entry(kind, tc.get("start"), tc.get("end"), continuity=cont, status=status, basis=tc.get("basis"),
                          note=tc.get("note") or tc.get("time_reference")))
    elif isinstance(tc, dict) and "first_acquisition" in tc and not out:
        # Raster koleksiyonu (ör. HD02): seçili edinimler — sürekli bir dönem DEĞİL.
        out.append(_entry("acquisition_date", tc.get("first_acquisition"), tc.get("last_acquisition"),
                          continuity="selected_acquisitions", status="SOURCE_DATA", basis=tc.get("basis"),
                          note=f"{tc.get('item_count')} selected acquisitions; not a continuous period"))
    if profile and not any(o["kind"] == "validity_period" for o in out):
        if profile.get("valid_from") or profile.get("valid_to"):
            out.append(_entry("validity_period", profile.get("valid_from"), profile.get("valid_to"), status="SOURCE_DECLARED",
                              basis=profile.get("source_file"), note=profile.get("temporal_note")))
        if profile.get("temporal_currency_year"):
            out.append(_entry("source_reference_date", str(profile["temporal_currency_year"]), None, continuity="single_date",
                              status="SOURCE_DECLARED", basis=f"{profile.get('source_file')}#temporal_currency_year"))
    return out


def summary_state(items: list[dict[str, Any]]) -> str:
    """present | partial | unknown — katalog tamlık özeti için."""
    if not items:
        return "unknown"
    known = [i for i in items if i["status"] != "UNKNOWN"]
    if not known:
        return "unknown"
    return "present" if len(known) == len(items) else "partial"
