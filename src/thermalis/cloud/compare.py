"""Yerel kayıt ile bulut kopyasının bilimsel karşılaştırması (taşıma doğrulaması; yalnız okuma).

Bulut kopyası, yerel katalogdan bağımsız geçici bir veri köküne yansıtılır (yalnız kayıtlar ve küçük JSON belgeler;
veri dosyaları indirilmez) ve aynı sözleşme işlevleriyle her katman için görünüm üretilir. Yerel anlamsal doğrulama
günlüğü geçici köke kopyalanır (geçişten sonra da aynı günlük geçerli olacaktır).

Beklenen farklar ayrı raporlanır ve bilimsel içerik sayılmaz:
- depolama yeri (``local`` ↔ ``r2``),
- ``RECORD_FILES_WRITABLE`` (eski yerel kayıt yazılabilir; bulut yansısı salt okunur),
- içerik kimliği kaynağı (önbellek ↔ yayınla saklanan ek kayıt).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..objectstore.base import ObjectStore
from ..objectstore.publication import read_commit, release_prefix

EXPECTED_ISSUE_DIFFERENCES = {"RECORD_FILES_WRITABLE"}


def _views(root: Path, dataset_id: str, version: str) -> dict[str, dict[str, Any]]:
    from ..data import contract
    from ..data.catalog_index import _query, query_record_issues

    out: dict[str, dict[str, Any]] = {}
    layers = _query(root, "SELECT layer_dataset_id FROM layers WHERE dataset_id=? AND version=? ORDER BY layer_dataset_id",
                    (dataset_id, version))
    for row in layers:
        lid = row["layer_dataset_id"]
        d = contract.descriptor(root, lid, version)
        q = contract.quality(root, lid, version)
        sem = contract.semantic_dictionary(root, lid, version)
        domains = {f["domain_name"] for f in sem["fields"] if f.get("domain_name")}
        out[lid] = {
            "dataset_version": d["dataset_version"], "content_id": d["content"]["content_id"],
            "content_algorithm": d["content"]["algorithm"], "package": {k: d["package"][k] for k in ("package_id", "fingerprint_sha256")},
            "quality_status": d["quality"]["status"], "access": d["access"], "crs": d["crs"], "schema": d["schema"],
            "temporal": d["temporal"], "layer": d["layer"], "geometry": d.get("geometry"),
            "findings": [(f["seq"], f["exception_id"], f["category"], f["disposition"], f["evidence"]) for f in q["findings"]],
            "special_records": q["special_records"], "gates": q["gates"],
            "semantics": [(f["field"], f["status"], f["meaning"], f["unit"], f["unit_status"], f["null_meaning"], f["domain_name"],
                           f["verified_by"]) for f in sem["fields"]],
            "domains": {n: contract.domain_codes(root, lid, version, n)["codes"] for n in sorted(domains)},
            "record_issues": sorted((i["code"], i["detail"] or "") for i in query_record_issues(root, dataset_id, version)
                                    if i["code"] not in EXPECTED_ISSUE_DIFFERENCES),
        }
    return out


def compare_release(local_root: Path, store: ObjectStore, dataset_id: str, version: str, *, provider: str, bucket: str,
                    namespace: str = "") -> dict[str, Any]:
    from ..data import semantics
    from ..data.catalog_index import rebuild_index
    from .catalog_sync import mirror_release

    prefix = release_prefix(dataset_id, version)
    commit = read_commit(store, prefix)
    if commit is None:
        return {"status": "FAIL", "reason": "release_not_committed"}
    tmp = Path(tempfile.mkdtemp(prefix="thermalis-compare-"))
    try:
        cloud_root = tmp / "CLOUD"
        (cloud_root / "data").mkdir(parents=True)
        mirror_release(store, cloud_root, prefix, commit, provider=provider, bucket=bucket, namespace=namespace)
        log = semantics.verifications_path(local_root)
        if log.is_file():
            target = semantics.verifications_path(cloud_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(log, target)
        rebuild_index(cloud_root)
        local_view, cloud_view = _views(local_root, dataset_id, version), _views(cloud_root, dataset_id, version)
    finally:
        for p in tmp.rglob("*"):
            if p.is_file():
                p.chmod(0o600)
        shutil.rmtree(tmp, ignore_errors=True)
    differences = []
    for lid in sorted(set(local_view) | set(cloud_view)):
        a, b = local_view.get(lid), cloud_view.get(lid)
        if a is None or b is None:
            differences.append({"layer": lid, "field": "*", "detail": "layer_missing"})
            continue
        for key in sorted(set(a) | set(b)):
            if a.get(key) != b.get(key):
                differences.append({"layer": lid, "field": key})
    compared = sorted(next(iter(local_view.values()), {}).keys())
    return {"status": "PASS" if not differences and local_view else "FAIL", "layers": sorted(local_view),
            "compared_fields": compared, "differences": differences,
            "expected_differences": ["storage_location", "content_id_source", *sorted(EXPECTED_ISSUE_DIFFERENCES)],
            "commit_operation": commit["operation_id"]}
