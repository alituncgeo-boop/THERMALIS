"""Sürüm başına yetkili kaynak (release authority): yerel kayıt mı, işaretli bulut (R2) sürümü mü.

Genel depo arka ucu (``storage.backend``) DEĞİŞMEZ; karar yalnız tek bir sürüm için verilir. Bir sürümün yerel kaydı
ile işaretli bulut kopyası aynı anda bulunduğunda katalog varsayılan olarak yerel kaydı esas alır; bu modülle,
denklik doğrulandıktan sonra bulut sürümü yetkili yapılabilir ve gerektiğinde (geri alma) yerel kayda dönülebilir.

Kurallar:
- ``r2`` kararı yalnız denklik kontrolleri GEÇERSE kaydedilir: kayıt dosyaları (version/quality/integrity) bayt
  düzeyinde aynı; işaretteki yük nesneleri yerel bütünlük listesiyle (yol + boyut + SHA-256) birebir; paket parmak
  izi aynı; katman içerik kimlikleri (bulutta saklanan ``layer_facts.json``) yerel kopyadan türetilenlerle aynı;
  istenirse buluttaki her nesne baştan sona yeniden okunur (tam SHA-256).
- Kararlar bulut defterinde (``authority`` akışı) değişmez kayıtlar olarak tutulur; en son karar geçerlidir. Geri alma
  yeni bir ``local`` kararıdır (geçmiş silinmez).
- Yerel sürüm klasörü hiçbir koşulda silinmez, taşınmaz veya değiştirilmez; yalnız katalogda ``release_copies``
  tablosunda ikincil kopya olarak görünür.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..data import catalog
from . import ledger

SCHEMA = "thermalis.release-authority/1"
SOURCES = ("r2", "local")
RECORD_FILES = ("version.json", "quality.json", "integrity.json")


class AuthorityError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_authority(data_root: Path) -> dict[str, dict[str, Any]]:
    """``"<veri kimliği>@<sürüm>" → en son karar`` (karar zamanı, ardından kimlik sırasıyla)."""
    try:
        items = ledger.entries(data_root, "authority")
    except ledger.LedgerError:
        return {}
    by_key: dict[str, list[dict[str, Any]]] = {}
    for d in items:
        if d.get("schema") != SCHEMA or d.get("source") not in SOURCES or d.get("id") != d.get("_ledger_id"):
            continue
        if ledger.content_id({k: v for k, v in d.items() if k not in ("id", "_ledger_id")}) != str(d["id"])[len("authority-"):]:
            continue  # içeriği değiştirilmiş kayıt yok sayılır
        by_key.setdefault(f"{d['dataset_id']}@{d['version']}", []).append(d)
    # Her karar yerini aldığı kararı (``previous_decision``) gösterir: zincirin başı geçerlidir (aynı saniyede verilen
    # kararlar da doğru sıralanır). Çatallanma olursa (iki makine aynı karardan ayrıldıysa) en son zamanlı baş seçilir.
    latest: dict[str, dict[str, Any]] = {}
    for key, decisions in by_key.items():
        replaced = {d.get("previous_decision") for d in decisions}
        heads = [d for d in decisions if d["id"] not in replaced] or decisions
        latest[key] = max(heads, key=lambda d: (str(d.get("decided_at") or ""), str(d["id"])))
    return latest


def history(data_root: Path, dataset_id: str, version: str) -> list[dict[str, Any]]:
    """Kararlar eskiden yeniye (zincir sırası: baştan geriye ``previous_decision`` izlenir)."""
    items = {d["id"]: d for d in ledger.entries(data_root, "authority")
             if d.get("schema") == SCHEMA and d.get("dataset_id") == dataset_id and d.get("version") == version and d.get("id")}
    head = load_authority(data_root).get(f"{dataset_id}@{version}")
    chain: list[dict[str, Any]] = []
    while head is not None and head["id"] not in {c["id"] for c in chain}:
        chain.append(head)
        head = items.get(head.get("previous_decision") or "")
    return list(reversed(chain))


def verify_equivalence(data_root: Path, dataset_id: str, version: str, *, store=None) -> list[dict[str, Any]]:
    """Yerel kayıt ile işaretli bulut kopyasının denkliği. Her kontrol ``{"id","status","detail"}``."""
    from ..data.catalog_index import FACTS_CACHE, _facts_for, _registered_fingerprint
    from .catalog_sync import LOCATION_NAME, mirror_folder

    checks: list[dict[str, Any]] = []

    def add(cid: str, ok: bool | None, detail: str) -> None:
        checks.append({"id": cid, "status": "PASS" if ok else ("NOT_RUN" if ok is None else "FAIL"), "detail": detail})

    local = catalog.version_dir(data_root, dataset_id, version)
    mirror = mirror_folder(data_root, dataset_id, version)
    has_local = (local / "version.json").is_file()
    has_mirror = (mirror / "version.json").is_file() and (mirror / LOCATION_NAME).is_file() and (mirror / "_COMMIT.json").is_file()
    add("EQ-01", has_local and has_mirror, f"local={has_local} cloud_mirror={has_mirror}")
    if not (has_local and has_mirror):
        return checks
    lrec, mrec = _read(local / "version.json"), _read(mirror / "version.json")
    add("EQ-02", lrec.get("registration", {}).get("status") == "REGISTERED" and mrec.get("registration", {}).get("status") == "REGISTERED",
        "both records REGISTERED")
    same = [n for n in RECORD_FILES if (local / n).is_file() and (mirror / n).is_file() and _sha(local / n) == _sha(mirror / n)]
    add("EQ-03", len(same) == len(RECORD_FILES), f"byte-identical records: {same}")
    lfp, mfp = _registered_fingerprint(local, lrec)[0], _registered_fingerprint(mirror, mrec)[0]
    add("EQ-04", bool(lfp) and lfp == mfp, f"package fingerprint local={lfp} cloud={mfp}")
    commit = _read(mirror / "_COMMIT.json")
    prefix = _read(mirror / LOCATION_NAME).get("release_prefix") or ""
    payload = {o["key"][len(prefix + "payload/"):]: (o["size"], o["sha256"]) for o in commit.get("objects", [])
               if o["key"].startswith(prefix + "payload/")}
    files = {f["path"]: (f["bytes"], f["sha256"]) for f in _read(local / "integrity.json").get("files", [])}
    missing = sorted(set(files) - set(payload))
    differ = sorted(p for p in set(files) & set(payload) if files[p] != payload[p])
    # Bütünlük listesinde olmayan paket denetim dosyaları (ör. MANIFEST, SHA256SUMS, hazır işareti) yerel yük klasöründeki
    # dosyayla bayt bayt karşılaştırılır; yerelde karşılığı olmayan veya farklı olan nesne denkliği bozar.
    extra = sorted(set(payload) - set(files))
    unmatched = []
    for rel in extra:
        p = local / "payload" / Path(*rel.split("/"))
        if not p.is_file() or (p.stat().st_size, _sha(p)) != payload[rel]:
            unmatched.append(rel)
    add("EQ-05", bool(files) and not missing and not differ and not unmatched,
        f"{len(files)} integrity-listed files + {len(extra) - len(unmatched)} package control files identical to "
        f"{len(payload)} committed payload objects; missing={missing[:5]} different={differ[:5]} unmatched={unmatched[:5]}")
    add("EQ-06", commit.get("dataset_id") == dataset_id and commit.get("version") == version
        and commit.get("verification") in ("full_sha256_read_back",), f"commit verification={commit.get('verification')}")
    # Katman içerik kimlikleri: buluttaki ek kayıt ile yerel kopyadan türetilen (önbellek / salt okunur tarama).
    cache_path = catalog.data_dir(data_root) / FACTS_CACHE
    cache = _read(cache_path) if cache_path.is_file() else {}
    lfacts, lsrc = _facts_for(data_root, local, lrec, cache)
    mfacts, msrc = _facts_for(data_root, mirror, mrec, {})

    def ids(facts: dict[str, Any]) -> dict[str, Any]:
        return {n: (f or {}).get("content_id") for n, f in facts.items()}

    add("EQ-07", bool(lfacts) and ids(lfacts) == ids(mfacts) and all(ids(lfacts).values()),
        f"{len(lfacts)} layer content IDs local({lsrc}) vs cloud({msrc}); equal={ids(lfacts) == ids(mfacts)}")
    if store is None:
        add("EQ-08", None, "full read-back not requested")
    else:
        from ..objectstore.publication import inspect_release

        state = inspect_release(store, prefix, full=True)
        add("EQ-08", state.status == "COMMITTED", f"full SHA-256 read-back of {state.object_count} objects: {state.status}")
    return checks


def build_decision(*, dataset_id: str, version: str, source: str, decided_by: str, reason: str,
                   checks: list[dict[str, Any]] | None = None, previous: str | None = None,
                   previous_decision: str | None = None, decided_at: str | None = None) -> dict[str, Any]:
    if source not in SOURCES:
        raise AuthorityError("authority_source_invalid", source)
    if not decided_by or not reason:
        raise AuthorityError("authority_decision_incomplete", "decided_by and reason required")
    body = {"schema": SCHEMA, "dataset_id": dataset_id, "version": version, "source": source, "decided_by": decided_by,
            "reason": reason, "decided_at": decided_at or _now(), "previous_source": previous,
            "previous_decision": previous_decision, "equivalence_checks": checks or []}
    body["id"] = "authority-" + ledger.content_id(body)
    return body


def set_authority(data_root: Path, dataset_id: str, version: str, source: str, *, decided_by: str, reason: str,
                  store=None, require_read_back: bool = True) -> dict[str, Any]:
    """Kararı doğrular, defterde kaydeder ve dizini yeniden kurar. Yerel klasöre dokunulmaz."""
    from ..data.catalog_index import rebuild_index
    from .catalog_sync import mirror_folder

    head = load_authority(data_root).get(f"{dataset_id}@{version}", {})
    current = head.get("source", "local")
    checks: list[dict[str, Any]] = []
    if source == "r2":
        if require_read_back and store is None:
            raise AuthorityError("authority_read_back_required", "a store is required to re-read every committed object")
        checks = verify_equivalence(data_root, dataset_id, version, store=store)
        failed = [c for c in checks if c["status"] == "FAIL" or (c["status"] == "NOT_RUN" and require_read_back)]
        if failed:
            raise AuthorityError("authority_equivalence_failed", json.dumps(failed, ensure_ascii=False))
    else:
        local = catalog.version_dir(data_root, dataset_id, version)
        if not (local / "version.json").is_file():
            raise AuthorityError("authority_local_record_missing", str(local))
        checks = [{"id": "RB-01", "status": "PASS", "detail": "local record present (rollback target)"}]
        if not (mirror_folder(data_root, dataset_id, version) / "version.json").is_file():
            checks.append({"id": "RB-02", "status": "PASS", "detail": "no cloud mirror present"})
    decision = build_decision(dataset_id=dataset_id, version=version, source=source, decided_by=decided_by, reason=reason,
                              checks=checks, previous=current, previous_decision=head.get("id"))
    ledger.record_local(data_root, "authority", decision["id"], decision)
    if store is not None:
        ledger.push_pending(store, data_root)
    rebuild_index(data_root)
    return decision
