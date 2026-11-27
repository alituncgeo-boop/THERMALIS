"""Yerel denetim günlükleri ↔ bulut defteri.

- Anlamsal doğrulama günlüğü (``_semantic/verifications.jsonl``): SHA-256 zincirli. Her satır değişmeden deftere
  ``<sıra:08d>-<entry_hash[:16]>`` kimliğiyle yazılır. Boş/eksik yerel günlük, defterdeki zincir sırayla ve zincir
  denetimiyle geri kurulur (kayıtlar yeniden üretilmez: özgün satırlar aynen yazılır). Yerel zincir defterle
  çatışıyorsa (farklı makinede farklı dal) yerel günlüğe DOKUNULMAZ; çatışma raporlanır.
- Yeniden doğrulama geçmişi (``_ingest/verifications.jsonl``): zincirsiz; her satır içerik özetiyle tekil.
  Eksik satırlar eklenir (sıra: zaman damgası).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import ledger


def _semantic_path(data_root: Path) -> Path:
    from ..data.semantics import verifications_path

    return verifications_path(data_root)


def _reverify_path(data_root: Path) -> Path:
    from ..data import catalog

    return catalog.data_dir(data_root) / "_ingest" / "verifications.jsonl"


def _line_id(line: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(line, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:32]


def export_local_logs(data_root: Path) -> int:
    """Yerel günlüklerin deftere henüz yazılmamış satırlarını yerel deftere (+ itme kuyruğuna) kaydeder."""
    from ..data.semantics import read_log

    n = 0
    valid, _ = read_log(data_root)
    known = {p.stem for p in ledger.ledger_dir(data_root, "semantic_verifications").glob("*.json")} \
        if ledger.ledger_dir(data_root, "semantic_verifications").is_dir() else set()
    for item in valid:
        entry_id = f"{int(item['seq']):08d}-{item['entry_hash'][:16]}"
        if entry_id not in known:
            ledger.record_local(data_root, "semantic_verifications", entry_id, item)
            n += 1
    path = _reverify_path(data_root)
    known = {p.stem for p in ledger.ledger_dir(data_root, "reverifications").glob("*.json")} \
        if ledger.ledger_dir(data_root, "reverifications").is_dir() else set()
    for raw in path.read_text(encoding="utf-8").splitlines() if path.is_file() else []:
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        entry_id = _line_id(line)
        if entry_id not in known:
            ledger.record_local(data_root, "reverifications", entry_id, line)
            known.add(entry_id)
            n += 1
    return n


def restore_local_logs(data_root: Path) -> dict[str, Any]:
    """Defterde olup yerel günlükte olmayan kayıtları geri kurar (yalnız güvenli durumlarda)."""
    from ..data.semantics import _entry_hash, read_log

    out: dict[str, Any] = {"semantic_restored": 0, "semantic_conflict": None, "reverifications_restored": 0}
    chain = sorted((e for e in ledger.entries(data_root, "semantic_verifications") if isinstance(e.get("seq"), int)),
                   key=lambda e: e["seq"])
    chain = [{k: v for k, v in e.items() if k != "_ledger_id"} for e in chain]
    valid, integrity = read_log(data_root)
    if chain and integrity["status"] != "broken":
        local_hashes = [v["entry_hash"] for v in valid]
        cloud_hashes = [c["entry_hash"] for c in chain]
        if local_hashes == cloud_hashes[: len(local_hashes)]:
            missing = chain[len(local_hashes):]
            prev = local_hashes[-1] if local_hashes else "0" * 64
            ok = []
            for item in missing:  # zincir denetimi: bozuk defter girdisi asla yazılmaz
                if item.get("prev_hash") != prev or _entry_hash(item) != item.get("entry_hash"):
                    out["semantic_conflict"] = f"ledger_chain_invalid_at_seq:{item.get('seq')}"
                    break
                ok.append(item)
                prev = item["entry_hash"]
            if ok:
                path = _semantic_path(data_root)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    for item in ok:
                        handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
                out["semantic_restored"] = len(ok)
        elif local_hashes[: len(cloud_hashes)] != cloud_hashes:
            out["semantic_conflict"] = "local_chain_diverges_from_ledger (local log untouched)"
    path = _reverify_path(data_root)
    present = set()
    for raw in path.read_text(encoding="utf-8").splitlines() if path.is_file() else []:
        try:
            present.add(_line_id(json.loads(raw)))
        except ValueError:
            continue
    missing = sorted((e for e in ledger.entries(data_root, "reverifications") if e["_ledger_id"] not in present),
                     key=lambda e: str(e.get("at")))
    if missing:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for e in missing:
                handle.write(json.dumps({k: v for k, v in e.items() if k != "_ledger_id"}, ensure_ascii=False) + "\n")
        out["reverifications_restored"] = len(missing)
    return out
