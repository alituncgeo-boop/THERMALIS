"""Denetlenebilir metadata düzeltmeleri (revision) — değişmez sürüm içeriğine dokunmadan.

Bir düzeltme, belirli bir sürümün (``dataset_id`` + ``version`` + temel commit işlem kimliği) kayıtlarına
(``version.json`` / ``quality.json``) uygulanan, gerekçeli ve kanıtlı bir **örtü**dür. Yük (payload) yeniden
yüklenmez, commit'li kayıtlar değişmez. Düzeltmeler bulut defterinde (``metadata_revisions``) yalnız-eklenen
değişmez girdilerdir; katalog dizini kurulurken "etkin kayıt = temel kayıt + düzeltmeler (sırayla)" uygulanır.

Değişiklik dili kasıtlı olarak dardır: ``{"record": "version.json", "path": "layers.X.raster.vertical_reference",
"op": "set", "value": ...}``. Önceki değer düzeltmede saklanır (``previous``) ve dizin her alanın kaynağını
(``source`` / ``revision:<kimlik>``) bilir. Bir düzeltme başka bir sürüme uygulanamaz; temel commit değişmişse
(farklı işlem kimliği) uygulanmaz ve raporlanır.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "thermalis.metadata-revision/1"
RECORDS = ("version.json", "quality.json")
PROTECTED_PATHS = ("dataset_id", "version", "registration", "source.package_fingerprint", "access.classification")


class RevisionError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def _get(doc: Any, path: str) -> Any:
    cur = doc
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _set(doc: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cur = doc
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            if part in cur and cur[part] is not None:
                raise RevisionError("revision_path_not_object", path)
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def build(*, dataset_id: str, version: str, base_operation_id: str, records: dict[str, dict[str, Any]], changes: list[dict[str, Any]],
          reason: str, evidence: list[dict[str, Any]], created_by: str, created_at: str | None = None) -> dict[str, Any]:
    """Düzeltme girdisi üretir; ``previous`` değerleri temel (etkin) kayıtlardan doldurulur."""
    from ..cloud.ledger import content_id

    if not reason or not evidence:
        raise RevisionError("revision_requires_reason_and_evidence")
    out_changes = []
    for c in changes:
        if c.get("record") not in RECORDS or c.get("op") != "set" or not c.get("path"):
            raise RevisionError("revision_change_invalid", json.dumps(c, ensure_ascii=False)[:200])
        if any(c["path"] == p or c["path"].startswith(p + ".") for p in PROTECTED_PATHS):
            raise RevisionError("revision_path_protected", c["path"])  # kimlik, kayıt durumu, parmak izi, erişim sınıfı
        out_changes.append({"record": c["record"], "path": c["path"], "op": "set", "value": c.get("value"),
                            "previous": _get(records.get(c["record"]) or {}, c["path"])})
    body = {"schema": SCHEMA, "dataset_id": dataset_id, "version": version, "base_operation_id": base_operation_id,
            "created_at": created_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "created_by": created_by,
            "reason": reason, "evidence": evidence, "changes": out_changes}
    body["id"] = "rev-" + content_id(body)
    return body


def for_release(data_root: Path, dataset_id: str, version: str) -> list[dict[str, Any]]:
    from ..cloud.ledger import content_id, entries

    revs = [e for e in entries(data_root, "metadata_revisions")
            if e.get("schema") == SCHEMA and e.get("dataset_id") == dataset_id and e.get("version") == version
            and e.get("id") == e.get("_ledger_id")
            # içeriği sonradan değiştirilmiş girdi uygulanmaz (kimlik içerikten türetilir)
            and str(e.get("id")) == "rev-" + content_id({k: v for k, v in e.items() if k not in ("id", "_ledger_id")})]
    return sorted(revs, key=lambda r: (str(r.get("created_at")), str(r.get("id"))))


def base_operation(folder: Path) -> str | None:
    """Kayıt klasörünün temel işlem kimliği: bulut yansısında commit, yerel sürümde kayıt (ingest) kimliği."""
    commit = folder / "_COMMIT.json"
    if commit.is_file():
        return json.loads(commit.read_text(encoding="utf-8")).get("operation_id")
    try:
        return json.loads((folder / "version.json").read_text(encoding="utf-8")).get("registration", {}).get("ingest_id")
    except (OSError, ValueError):
        return None


def effective(data_root: Path | None, folder: Path, name: str, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Etkin kayıt: temel kayıt + bu sürüme ait (aynı temel işleme bağlı) düzeltmeler, sırayla."""
    doc = base if base is not None else json.loads((folder / name).read_text(encoding="utf-8"))
    if data_root is None:
        return doc
    ds, ver = (doc.get("dataset_id"), doc.get("version")) if name == "version.json" else _identity(folder)
    if not ds:
        return doc
    revs = for_release(data_root, ds, ver)
    if not revs:
        return doc
    op = base_operation(folder)
    doc = copy.deepcopy(doc)
    applied = []
    for r in revs:
        if r.get("base_operation_id") not in (op, None) and op is not None:
            continue  # başka bir temel kayda yazılmış düzeltme bu kayda uygulanmaz
        for c in r.get("changes", []):
            if c.get("record") == name and c.get("op") == "set":
                _set(doc, c["path"], c.get("value"))
                applied.append({"revision": r["id"], "path": c["path"]})
    if applied:
        doc.setdefault("_revisions_applied", []).extend(applied)
    return doc


def record_folder(data_root: Path, dataset_id: str, version: str, target: str = "auto") -> Path:
    """Düzeltmenin bağlanacağı kayıt klasörü: ``r2`` (bulut yansısı), ``local`` veya ``auto`` (yetkili kaynak)."""
    from ..cloud.authority import load_authority
    from ..cloud.catalog_sync import mirror_folder
    from . import catalog

    local = catalog.version_dir(data_root, dataset_id, version)
    mirror = mirror_folder(data_root, dataset_id, version)
    if target == "auto":
        decided = load_authority(data_root).get(f"{dataset_id}@{version}", {}).get("source")
        target = "r2" if decided == "r2" or not (local / "version.json").is_file() else "local"
    folder = mirror if target == "r2" else local
    if not (folder / "version.json").is_file():
        raise RevisionError("revision_base_record_missing", str(folder))
    return folder


def create(data_root: Path, *, dataset_id: str, version: str, changes: list[dict[str, Any]], reason: str,
           evidence: list[dict[str, Any]], created_by: str, target: str = "auto") -> dict[str, Any]:
    """Etkin kayıtlardan ``previous`` değerleriyle düzeltme üretir ve yerel deftere (+ itme kuyruğu) yazar."""
    from ..cloud.ledger import record_local

    folder = record_folder(data_root, dataset_id, version, target)
    op = base_operation(folder)
    if not op:
        raise RevisionError("revision_base_operation_unknown", str(folder))
    records = {n: effective(data_root, folder, n) for n in RECORDS if (folder / n).is_file()}
    body = build(dataset_id=dataset_id, version=version, base_operation_id=op, records=records, changes=changes,
                 reason=reason, evidence=evidence, created_by=created_by)
    record_local(data_root, "metadata_revisions", body["id"], body)
    return body


def _identity(folder: Path) -> tuple[str | None, str | None]:
    try:
        v = json.loads((folder / "version.json").read_text(encoding="utf-8"))
        return v.get("dataset_id"), v.get("version")
    except (OSError, ValueError):
        return None, None
