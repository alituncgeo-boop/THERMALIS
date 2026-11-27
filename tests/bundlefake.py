"""Sentetik ZIP aktarım paketi (gerçek veri değil): GDB + GeoTIFF + iki bölümlü CSV + iç içe arşiv, iç manifest ve yan dosya."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

from rasterfake import cog_bytes
from thermalis.cloud.synthetic import build_gdb

CSV = ("latitude,longitude,elevation,utc_offset_seconds,timezone,timezone_abbreviation\n"
       "41.0,29.0,36.0,0,GMT,GMT\n\n"
       "time,temperature_2m (°C),wind_speed_10m (km/h),shortwave_radiation (W/m²),\"label (Aile, akraba vb.)\"\n"
       + "".join(f"2020-06-01T{h:02d}:00,{20 + h / 10:.1f},{h % 7}.5,{max(0, 600 - abs(12 - h) * 60)},x\n" for h in range(24)))


def build_bundle(root: Path, *, name: str = "SYN07_BUNDLE_v1.0.0-rc1", corrupt_member: bool = False, unlisted: bool = False,
                 bad_sidecar: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    staging = root.parent / f".stage-{name}"
    gdb = staging / "SOURCE_ORIGINAL" / "Synthetic.gdb"
    gdb.parent.mkdir(parents=True, exist_ok=True)
    build_gdb(gdb)
    members: dict[str, bytes] = {p.relative_to(staging).as_posix(): p.read_bytes() for p in sorted(gdb.rglob("*")) if p.is_file()}
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w", zipfile.ZIP_STORED) as nz:
        nz.writestr("tile/DEM.bin", b"\x00" * 2048)
    members.update({
        "SOURCE_ORIGINAL/SOURCE/SERIES.csv": CSV.encode("utf-8"),
        "PREPARED_INPUT/SURFACE_30M.tif": cog_bytes(seed=5),
        "SOURCE_ORIGINAL/ORIGINAL_DOWNLOADS/tile.DEM.zip": nested.getvalue(),
        "RELEASE_STATUS.json": json.dumps({"source_qa": "QA_CONDITIONAL", "status": "QA_CONDITIONAL"}).encode(),
        "PROVENANCE.json": json.dumps({"documented_tile_observation_span": {"from": "2011-03-10T16:05:11Z", "to": "2014-08-26T16:06:41Z"}}).encode(),
        "LICENSE_AND_ACCESS.md": "# Lisans\nSentetik test.\n".encode("utf-8"),
        "ANALYSIS_LIMITATIONS.md": "# Sınırlamalar\n- örnek\n".encode("utf-8"),
    })
    manifest = {"format": "thermalis-transfer-manifest-v1", "artifact_kind": "synthetic_bundle", "release_status": "QA_CONDITIONAL",
                "files": [{"path": p, "bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()} for p, b in sorted(members.items())]}
    if corrupt_member:
        manifest["files"][0]["sha256"] = "0" * 64
    archive = root / f"{name}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for p, b in sorted(members.items()):
            zf.writestr(zipfile.ZipInfo(p, (2026, 11, 26, 0, 0, 0)), b,
                        compress_type=zipfile.ZIP_STORED if p.endswith(".zip") else zipfile.ZIP_DEFLATED)
        zf.writestr("MANIFEST.json", json.dumps(manifest, indent=1))
        if unlisted:
            zf.writestr("EXTRA.txt", "not listed")
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    (root / f"{name}_ZIP_SHA256.txt").write_text(f"{'1' * 64 if bad_sidecar else sha}  {archive.name}\n", encoding="utf-8")
    (root / "TECHNICAL_DOCUMENTATION.md").write_text("# Teknik\n", encoding="utf-8")
    import shutil

    shutil.rmtree(staging)
    return root


def bundle_declaration(root: Path, **override) -> dict:
    from thermalis.data import declaration as decl
    from thermalis.data.bundle import load_bundle

    pkg = load_bundle(root)
    body = {"schema": decl.SCHEMA, "package_id": pkg.package_id, "manifest_sha256": pkg.manifest_sha256,
            "dataset_id": "THERMALIS_DATA/SYN07_BUNDLE/TESTKENT", "version": "1.0.0-rc1", "access_class": "PUBLIC",
            "access_basis": "synthetic", "source": {"product": "Synthetic bundle", "citation": "Synthetic (test)."},
            "supplementary_records": {"license": "not_declared"},
            "quality": {"source_status_ref": "RELEASE_STATUS.json#source_qa", "cross_check_refs": ["MANIFEST.json#release_status"],
                        "limitations_documents": ["package/ANALYSIS_LIMITATIONS.md"]},
            "bundle": {"tables": ["SOURCE_ORIGINAL/SOURCE/SERIES.csv"],
                       "roles": {"PREPARED_INPUT/SURFACE_30M.tif": "prepared_dsm_primary", "Districts": "reference_layer"}},
            "temporal": {"semantics": "source_acquisition_span", "start_ref": "PROVENANCE.json#documented_tile_observation_span.from",
                         "end_ref": "PROVENANCE.json#documented_tile_observation_span.to"},
            "declared_by": "test", "declared_at": "2026-11-27T00:00:00Z"}
    body.update(override)
    return decl.validate(body)
