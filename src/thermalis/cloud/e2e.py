"""Canlı uçtan uca bulut kabul testi (YALNIZ ELLE; açık onay + THERMALIS_R2_LIVE_TEST=1).

Tamamen yalıtılmıştır:
- R2'de benzersiz ``e2e-tests/<çalıştırma>/`` ad alanı (üretim ``incoming/``, ``releases/``, ``catalog/`` önekleri kullanılmaz),
- geçici bir veri kökü (gerçek katalog, HD01 ve yerel kayıtlar okunmaz/yazılmaz),
- yalnız sentetik paketler (rastgele değil; küçük, uydurma geometri; erişim sınıfı açıkça PUBLIC beyan edilir).
Uygulama silme sunmadığı için test nesneleri kalır; ``e2e-tests/`` önekine yaşam döngüsü kuralı önerilir.
"""

from __future__ import annotations

import io
import json
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..objectstore.base import ObjectStore
from ..objectstore.errors import StorageError
from ..objectstore.prefixed import PrefixedStore
from ..objectstore.publication import committed_releases, inspect_release, release_prefix
from . import layout


@dataclass
class Step:
    name: str
    status: str
    detail: str = ""


def run_e2e(base_store: ObjectStore, *, provider: str, bucket: str, run_id: str | None = None) -> list[Step]:
    from ..data import contract
    from .catalog_sync import cloud_record_folders, sync_catalog
    from .intake import CloudIntake
    from .synthetic import build_package
    from .upload import ProgressReporter, UploadRejected, plan_package, upload_package

    run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    namespace = f"e2e-tests/{run_id}/"
    store = PrefixedStore(base_store, namespace)
    steps: list[Step] = []
    ds = "THERMALIS_DATA/SYN99_E2E_SYNTHETIC/TESTKENT"
    public = {"not_released_as_public_open_data": None, "access_classification": "PUBLIC"}

    def step(name: str, fn) -> bool:
        try:
            steps.append(Step(name, "PASS", str(fn() or "")))
            return True
        except (StorageError, UploadRejected, AssertionError, KeyError, ValueError) as exc:
            steps.append(Step(name, "FAIL", f"{type(exc).__name__}: {exc}"))
            return False

    with tempfile.TemporaryDirectory(prefix="thermalis-e2e-") as tmp_name:
        tmp = Path(tmp_name)
        data_root = tmp / "THERMALIS_DATA"  # yalıtılmış geçici katalog
        data_root.mkdir()
        pkg = build_package(tmp / "src" / "SYN99_E2E_v1.0.0", version="1.0.0", dataset_id=ds, package_id="SYN99_E2E_v1.0.0",
                            release_overrides=public)
        plan = plan_package(pkg)
        processor = CloudIntake(store, store, data_root, provider=provider, bucket=bucket, intake_bucket=bucket,
                                namespace=namespace, staging_root=tmp / "staging", staging_max_bytes=10 * 1024 ** 3)

        def upload():
            out = io.StringIO()
            r = upload_package(store, pkg, provider=provider, bucket=bucket, plan=plan,
                               reporter=ProgressReporter(plan.total_bytes, len(plan.files), out=out))
            assert r["status"] == "UPLOADED", r
            return f"{r['files']} dosya, {r['bytes']} B; THERMALIS_READY.json son yazıldı"

        def duplicate_upload():
            r = upload_package(store, pkg, provider=provider, bucket=bucket, plan=plan)
            assert r["status"] == "ALREADY_UPLOADED", r
            return "yineleme algılandı; hiçbir şey yüklenmedi"

        def incomplete_invisible():
            store.put_bytes(layout.announcement_key("SYN99_E2E_INCOMPLETE"), b"{}", create_only=True, operation_id=f"e2e-{run_id}")
            outcome = processor.process("SYN99_E2E_INCOMPLETE", "-")
            assert (outcome.status, outcome.code) == ("SKIPPED", "INCOMPLETE"), outcome
            return "işaretsiz paket işlenmedi"

        def accept():
            scan = processor.scan()
            statuses = {o.package_id: o.status for o in scan.outcomes}
            assert statuses.get(plan.package_id) == "ACCEPTED", scan.outcomes
            return f"tarama={scan.status}, sonuçlar={statuses}"

        def committed_verified():
            state = inspect_release(store, release_prefix(ds, "1.0.0"), full=True)
            assert state.status == "COMMITTED", state
            return f"{state.object_count} nesne tam SHA-256 geri okuma ile doğrulandı"

        def catalog_visible():
            assert len(cloud_record_folders(data_root)) == 1
            d = contract.descriptor(data_root, f"{ds}/NEIGHBORHOODS")
            assert d["storage"]["backend"] == "r2" and d["quality"]["status"] == "QA_CONDITIONAL"
            assert contract.access(data_root, f"{ds}/NEIGHBORHOODS")["absolute_path"] is None
            return "geçici katalogda bulut sürümü (QA_CONDITIONAL, konum r2, yerel yol yok)"

        def quiet_rescan():
            intake_file = data_root / "data" / "_ingest" / "intake.json"
            before = intake_file.read_bytes()
            scan = processor.scan()
            # Sonuçlanmış paket yeniden işlenmez; işareti henüz gelmemiş paket yalnız denetlenir (durum değişmez).
            assert all(o.package_id == "SYN99_E2E_INCOMPLETE" and o.status == "SKIPPED" for o in scan.outcomes), scan
            assert scan.sync["unchanged"] == 1 and scan.sync["mirrored"] == [], scan
            assert intake_file.read_bytes() == before, "intake.json rewritten"
            return "sonuçlanmış paket yeniden işlenmedi; indirme ve durum yazımı yok"

        def rebuild():
            other = tmp / "REBUILT"
            other.mkdir()
            r = sync_catalog(store, other, provider=provider, bucket=bucket, namespace=namespace, full=True)
            assert r.mirrored == [f"{ds}@1.0.0"] and r.release_count == 1, r
            return "yalnız commit işaretlerinden tam yeniden kurulum"

        def conflict():
            other = build_package(tmp / "src2" / "SYN99_E2E_CONFLICT", version="1.0.0", dataset_id=ds, package_id="SYN99_E2E_CONFLICT",
                                  release_overrides=public, extra_field="AREA_HA")
            try:
                upload_package(store, other, provider=provider, bucket=bucket)
            except UploadRejected as exc:
                assert exc.code == "version_already_published", exc.code
                return "aynı sürüm farklı içerik: yükleyici reddetti"
            raise AssertionError("conflicting version was accepted")

        def visibility():
            prefixes = [p for p, c, problem in committed_releases(store) if c and not problem]
            assert prefixes == [release_prefix(ds, "1.0.0")], prefixes
            return "yalnız işaretli sürüm görünür"

        for name, fn in (("upload_package", upload), ("duplicate_upload", duplicate_upload),
                         ("incomplete_package_invisible", incomplete_invisible), ("cloud_intake_accept", accept),
                         ("commit_full_verification", committed_verified), ("central_catalog_visible", catalog_visible),
                         ("quiet_rescan", quiet_rescan), ("catalog_full_rebuild", rebuild), ("version_conflict_rejected", conflict),
                         ("committed_only_visibility", visibility)):
            if not step(name, fn) and name in ("upload_package", "cloud_intake_accept"):
                break  # sonraki adımlar bunlara bağlı
    steps.append(Step("namespace", "INFO", namespace))
    return steps


def _synthetic_authorization(plan_like: dict, provider: str, bucket: str) -> dict:
    from . import authorization as auth

    record = auth.template(provider=provider, bucket=bucket, **plan_like)
    record.update(granted_by="SYNTHETIC REHEARSAL — NOT A REAL GRANT", authority="SYNTHETIC",
                  document_reference="SYNTHETIC-NO-REAL-DATA", granted_at="2026-01-01T00:00:00Z")
    return record


def prepare_legacy_release(tmp: Path, data_root: Path, *, dataset_id: str, package_id: str, restricted: bool) -> None:
    """Sentetik paketi geçici kökte kaydeder ve HD01 benzeri eski biçime getirir (+ doğrulama, denetim, geçmiş)."""
    from ..data import contract, semantics
    from ..data.catalog_index import rebuild_index
    from ..data.ingest import run_ingest
    from ..data.register import verify_registered
    from ..storage import write_json_atomic
    from .synthetic import build_package, make_legacy_record, write_ready_marker

    incoming = tmp / f"IN-{package_id}"
    incoming.mkdir(parents=True)
    overrides = None if restricted else {"not_released_as_public_open_data": None, "access_classification": "PUBLIC"}
    write_ready_marker(build_package(incoming / package_id, version="1.0.0", dataset_id=dataset_id, package_id=package_id,
                                     release_overrides=overrides, globalid=True))
    summary = run_ingest(incoming, data_root, settle_seconds=0)
    assert all(r.technical == "PASS" for r in summary.results), [r.stop_reason for r in summary.results]
    make_legacy_record(data_root, dataset_id, "1.0.0")
    rebuild_index(data_root)  # eski kayıt: içerik kimlikleri türetilmiş önbellekte hesaplanır (HD01 ile aynı)
    lid = f"{dataset_id}/NEIGHBORHOODS"
    cid = contract.field_info(data_root, lid, "1.0.0", "YAKA")["content_id"]
    semantics.append_verification(data_root, {"dataset_id": lid, "version": "1.0.0", "field": "YAKA",
                                              "meaning": "Side code (synthetic rehearsal)", "verified_by": "rehearsal"},
                                  content_id=cid, auth_method="in_process")
    audit = data_root / "data" / "_audits" / f"aud-{package_id.lower()}"
    write_json_atomic(audit / "INDEPENDENT_AUDIT_RESULT.json", {"schema_version": "1.0", "audit_id": audit.name, "dataset_id": dataset_id,
                                                                "version": "1.0.0", "status": "INDEPENDENT_AUDIT_PENDING",
                                                                "reason": "synthetic rehearsal"})
    verify_registered(data_root, dataset_id, "1.0.0")
    rebuild_index(data_root)


def run_migration_rehearsal(base_store: ObjectStore, *, provider: str, bucket: str, run_id: str | None = None,
                            cache_root: Path | None = None) -> list[Step]:
    """Genel taşıma mekanizmasının canlı provası: HD01 benzeri eski biçimli SENTETİK kayıtlar, yalıtılmış ad alanı."""
    import hashlib

    from ..data import catalog, contract
    from ..data.catalog_index import _query, query_record_issues
    from ..objectstore.cache import ObjectCache
    from .access import materialize_asset
    from .catalog_sync import sync_catalog
    from .compare import compare_release
    from .migrate import MigrationRefused, migrate_release, plan_migration, verify_migration

    run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    namespace = f"e2e-tests/{run_id}/"
    store = PrefixedStore(base_store, namespace)
    steps: list[Step] = []
    public_ds, restricted_ds = "THERMALIS_DATA/SYN98_MIGRATION/TESTKENT", "THERMALIS_DATA/SYN97_MIGRATION/TESTKENT"

    def step(name: str, fn) -> bool:
        try:
            steps.append(Step(name, "PASS", str(fn() or "")))
            return True
        except (StorageError, MigrationRefused, AssertionError, KeyError, ValueError, OSError) as exc:
            steps.append(Step(name, "FAIL", f"{type(exc).__name__}: {exc}"))
            return False

    with tempfile.TemporaryDirectory(prefix="thermalis-mig-") as tmp_name:
        tmp = Path(tmp_name)
        data_root = tmp / "THERMALIS_DATA"
        data_root.mkdir()
        prepare_legacy_release(tmp, data_root, dataset_id=public_ds, package_id="SYN98_MIGRATION_v1.0.0", restricted=False)
        prepare_legacy_release(tmp, data_root, dataset_id=restricted_ds, package_id="SYN97_MIGRATION_v1.0.0", restricted=True)
        folder = catalog.version_dir(data_root, public_ds, "1.0.0")
        before = {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob("*") if p.is_file()}
        ctx: dict = {}

        def legacy_shape():
            rec = json.loads((folder / "version.json").read_text(encoding="utf-8"))
            assert not any("content" in l for l in rec["layers"].values()) and "package_fingerprint" not in rec["source"]
            codes = {i["code"] for i in query_record_issues(data_root, public_ds, "1.0.0")}
            assert {"PACKAGE_FINGERPRINT_NOT_RECORDED", "RECORD_FILES_WRITABLE"} <= codes, codes
            special = contract.quality(data_root, f"{public_ds}/NEIGHBORHOODS")["special_records"]
            assert special and all(s.get("globalid") for s in special), special
            return f"eski biçim (içerik kimliği/parmak izi kayıtta yok), {len(special)} özel kayıt GLOBALID'li"

        def plan():
            p = plan_migration(data_root, public_ds, "1.0.0", provider=provider, bucket=bucket)
            names = set(p.records)
            need = {"version.json", "quality.json", "integrity.json", "layer_facts.json", "quality_identifiers.json", "migration.json",
                    "semantic_verifications.jsonl", "reverification_history.jsonl"}
            assert need <= names and any(n.startswith("audits/") for n in names), sorted(names)
            ctx["plan"] = p
            return f"kuru çalıştırma: {len(p.files)} veri dosyası, {len(p.records)} kayıt, {p.total_bytes} B; uyarılar {p.warnings}"

        def restricted_needs_authorization():
            try:
                plan_migration(data_root, restricted_ds, "1.0.0", provider=provider, bucket=bucket)
            except MigrationRefused as exc:
                assert exc.code == "authorization_required", exc.code
            else:
                raise AssertionError("restricted release planned without authorization")
            return "RESTRICTED sürüm yetki kaydı olmadan reddedildi"

        def migrate():
            r = migrate_release(store, ctx["plan"])
            assert r["status"] == "MIGRATED", r
            return f"{r['objects']} nesne, {r['bytes']} B yayımlandı"

        def idempotent():
            again = migrate_release(store, plan_migration(data_root, public_ds, "1.0.0", provider=provider, bucket=bucket))
            assert again["status"] == "ALREADY_MIGRATED", again
            return "aynı işlem kimliği: yeniden yükleme yok"

        def verified():
            v = verify_migration(store, ctx["plan"])
            assert v["status"] == "PASS", v
            return f"{v['payload_files']} veri dosyası integrity.json ile birebir; kayıtlar birebir; tam geri okuma {v['full_readback']}"

        def compared():
            c = compare_release(data_root, store, public_ds, "1.0.0", provider=provider, bucket=bucket, namespace=namespace)
            assert c["status"] == "PASS", c["differences"]
            return f"{len(c['layers'])} katman · alanlar: {', '.join(c['compared_fields'])} · fark yok"

        def dual_period():
            sync_catalog(store, data_root, provider=provider, bucket=bucket, namespace=namespace)
            rows = _query(data_root, "SELECT backend FROM storage_locations WHERE dataset_id=?", (public_ds,))
            copies = _query(data_root, "SELECT same_fingerprint FROM cloud_copies WHERE dataset_id=?", (public_ds,))
            issues = {i["code"] for i in query_record_issues(data_root, public_ds, "1.0.0")}
            assert [r["backend"] for r in rows] == ["local"] and [c["same_fingerprint"] for c in copies] == [1], (rows, copies)
            assert "DUPLICATE_ACROSS_BACKENDS" not in issues
            return "katalog yerel kaydı kullanıyor; bulut kopyası doğrulanmış kopya olarak görünüyor (sorun değil)"

        def materialize():
            from ..data.vector_inspect import layer_facts

            cloud_only = tmp / "CLOUD_ONLY"
            cloud_only.mkdir()
            sync_catalog(store, cloud_only, provider=provider, bucket=bucket, namespace=namespace, full=True)
            cache = ObjectCache(cache_root or tmp / "cache", 1024 ** 3, protected_roots=[data_root])
            m = materialize_asset(cloud_only, store, cache, f"{public_ds}/NEIGHBORHOODS")
            local_cid = layer_facts(folder / "payload" / "01_ADMIN" / "SOURCE" / "Admin.gdb", "Neighborhoods")["content_id"]
            assert m["materialized"] and layer_facts(Path(m["path"]), "Neighborhoods")["content_id"] == local_cid, m
            q = contract.quality(cloud_only, f"{public_ds}/NEIGHBORHOODS")
            assert all(s.get("globalid") for s in q["special_records"])
            return f"buluttan {m['files']} dosya ({m['bytes']} B) doğrulanarak önbelleğe indirildi; GDAL içerik kimliği yerelle aynı; GLOBALID'ler korunmuş"

        def restricted_with_authorization():
            rfolder = catalog.version_dir(data_root, restricted_ds, "1.0.0")
            rec = json.loads((rfolder / "version.json").read_text(encoding="utf-8"))
            manifest_sha = hashlib.sha256((rfolder / "payload" / "PACKAGE" / "MANIFEST.json").read_bytes()).hexdigest()
            auth_path = tmp / "auth.json"
            auth_path.write_text(json.dumps(_synthetic_authorization({
                "dataset_id": restricted_ds, "version": "1.0.0", "package_id": rec["source"]["package_id"],
                "manifest_sha256": manifest_sha, "access_class": "RESTRICTED"}, provider, bucket)), encoding="utf-8")
            p = plan_migration(data_root, restricted_ds, "1.0.0", provider=provider, bucket=bucket, authorization_path=auth_path)
            r = migrate_release(store, p)
            v = verify_migration(store, p)
            assert r["status"] == "MIGRATED" and v["status"] == "PASS" and v["access_class"] == "RESTRICTED", (r, v)
            return "RESTRICTED sentetik sürüm (sentetik yetki kaydıyla) taşındı; erişim sınıfı RESTRICTED kaldı"

        def local_untouched():
            after = {p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.rglob("*") if p.is_file()}
            assert after == before
            return f"yerel sürüm klasörü ({len(before)} dosya) bayt bayt aynı"

        for name, fn in (("legacy_record_shape", legacy_shape), ("migration_plan_dry_run", plan),
                         ("restricted_requires_authorization", restricted_needs_authorization), ("migrate_release", migrate),
                         ("idempotent_rerun", idempotent), ("payload_and_records_identical", verified),
                         ("scientific_comparison", compared), ("dual_period_catalog", dual_period),
                         ("verified_materialization", materialize), ("restricted_with_authorization", restricted_with_authorization),
                         ("local_release_untouched", local_untouched)):
            if not step(name, fn) and name in ("migration_plan_dry_run", "migrate_release"):
                break
    steps.append(Step("namespace", "INFO", namespace))
    return steps


# --------------------------------------------------------------------------- raster koleksiyonu kabul testi (gerçek arşivler)

class _CountingStore(PrefixedStore):
    """Aralıklı okumada indirilen baytları sayar (yalnız ölçüm; davranış aynı)."""

    def __init__(self, inner: ObjectStore, prefix: str) -> None:
        super().__init__(inner, prefix)
        self.range_bytes = 0
        self.range_requests = 0

    def read_range(self, key, start, end):
        data = super().read_range(key, start, end)
        self.range_bytes += len(data)
        self.range_requests += 1
        return data


def run_collection_acceptance(base_store: ObjectStore, settings, *, source: Path, spec: Path, archives: list[str],
                              run_id: str | None = None) -> list[Step]:
    """Seçili GERÇEK arşivlerle canlı kabul: kopyasız alt küme, yalıtılmış ad alanı, kesinti + sürdürme, tam geri okuma,
    metadata doğruluğu (MTL ile), /vsisubfile/+/vsis3/ aralıklı okuma, üye malzemeleştirme ve sınırlı disk ölçümü.

    Yalnız ``e2e-tests/<çalıştırma>/`` altına yazar; üretim önekleri ve gerçek katalog kullanılmaz.
    """
    import hashlib
    import os
    import shutil
    import tarfile
    import time
    import tracemalloc

    from ..data import contract
    from ..data import declaration as decl
    from ..data.collection import load_source_package, subset_package
    from ..data.formats.raster import read_window
    from ..objectstore.base import CancelToken
    from ..objectstore.cache import ObjectCache
    from ..objectstore.credentials import load_credential
    from ..objectstore.errors import Cancelled
    from ..objectstore.s3 import gdal_vsis3_session
    from .access import describe_asset, materialize_member
    from .catalog_sync import sync_catalog
    from .direct import plan_collection, publish_collection, verify_collection
    from .intake import machine_tag

    run_id = run_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    namespace = f"e2e-tests/{run_id}/"
    store = _CountingStore(base_store, namespace)
    cloud = settings.cloud
    steps: list[Step] = []
    ctx: dict = {}

    def step(name: str, fn) -> bool:
        try:
            steps.append(Step(name, "PASS", str(fn() or "")))
            return True
        except Exception as exc:  # noqa: BLE001 - kabul raporu her hatayı görünür kılar
            steps.append(Step(name, "FAIL", f"{type(exc).__name__}: {str(exc)[:400]}"))
            return False

    def tree_bytes(path: Path) -> int:
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0

    with tempfile.TemporaryDirectory(prefix="thermalis-col-") as tmp_name:
        tmp = Path(tmp_name)
        data_root = tmp / "THERMALIS_DATA"
        data_root.mkdir()
        work_root = tmp / "work"
        free_before = shutil.disk_usage(tmp).free
        original = load_source_package(Path(source))
        before = {e.path: (original.path(e.path).stat().st_size, original.path(e.path).stat().st_mtime_ns) for e in original.entries}
        ds = "THERMALIS_DATA/E2E_COLLECTION/ACCEPTANCE"
        ver = f"0.0.1-acc.{run_id.split('-')[-1]}"

        def subset():
            pkg = subset_package(original, archives, tmp / "subset", f"E2E_COLLECTION_{run_id.split('-')[-1]}")
            body = json.loads(Path(spec).read_text(encoding="utf-8-sig"))
            body.update(schema=decl.SCHEMA, package_id=pkg.package_id, manifest_sha256=pkg.manifest_sha256, dataset_id=ds, version=ver,
                        declared_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        declared_by="THERMALIS live acceptance test (synthetic identity; real source archives)")
            ctx.update(pkg=pkg, declaration=decl.validate(body))
            return (f"{len(archives)} gerçek arşiv ({sum(pkg.path(a).stat().st_size for a in archives) / 1e9:.2f} GB) yerinde; "
                    f"yalnız alt küme MANIFEST.json yazıldı (kopya yok)")

        def plan():
            tracemalloc.start()
            t0 = time.time()
            p = plan_collection(ctx["pkg"].root, ctx["declaration"], data_root=data_root, work_root=work_root, provider=cloud.provider,
                                bucket=cloud.bucket or "", package=ctx["pkg"])
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            ctx["plan"] = p
            s = p.summary()
            assert s["items"] == len(archives) and s["raster_assets"] > 0, s
            return (f"{s['items']} sahne · {s['assets']} üye · {s['raster_assets']} raster · {time.time() - t0:.0f} s · "
                    f"Python bellek tepe {peak / 1e6:.0f} MB · çalışma klasörü {tree_bytes(work_root) / 1e6:.2f} MB · "
                    f"kalite {s['quality_status']} ({s['source_status']}) · sorun {s['issue_count']}")

        def interrupted_then_resumed():
            p = ctx["plan"]
            cancel = CancelToken()
            seen = {"n": 0}

            def stop_midway(stage: str, done: int, total: int) -> None:
                seen["n"] = done
                if stage == "upload" and done >= min(total // 3, 300 * 1024 * 1024):
                    cancel.cancel()
            try:
                publish_collection(store, p, machine=machine_tag(), cancel=cancel, progress=stop_midway)
                raise AssertionError("publication was not interrupted")
            except Cancelled:
                pass
            first = seen["n"]
            sent = {"upload": 0}
            last = {"upload": 0}

            def count(stage: str, done: int, total: int) -> None:
                if stage == "upload":
                    sent["upload"] += done - last["upload"]
                    last["upload"] = done
            t0 = time.time()
            r = publish_collection(store, p, machine=machine_tag(), progress=count)
            assert r["status"] == "PUBLISHED", r
            ctx["publish_seconds"] = time.time() - t0
            return (f"ilk deneme {first / 1e6:.0f} MB sonra iptal edildi; aynı komut sürdürdü ve tamamladı "
                    f"({r['objects']} nesne, {r['bytes'] / 1e9:.2f} GB; tam geri okuma + _COMMIT.json, {ctx['publish_seconds']:.0f} s)")

        def idempotent():
            r = publish_collection(store, ctx["plan"], machine=machine_tag())
            assert r["status"] == "ALREADY_PUBLISHED", r
            return "yeniden çalıştırma: yükleme yok (ALREADY_PUBLISHED)"

        def verified():
            v = verify_collection(store, ctx["plan"], full=False)
            assert v["status"] == "PASS", v
            return f"{v['payload_files']} payload dosyası manifestle birebir; kayıtlar birebir; erişim {v['access_class']}"

        def catalog_and_metadata():
            sync = sync_catalog(store, data_root, provider=cloud.provider, bucket=cloud.bucket or "", namespace=namespace)
            assert sync.status == "ok" and sync.mirrored, sync
            lid = f"{ds}/{ctx['declaration']['collection']['layer_name']}"
            col = contract.collection(data_root, lid)
            assert col["item_count"] == len(archives), col["item_count"]
            checked = 0
            for a in archives:
                with tarfile.open(ctx["pkg"].path(a), "r:") as t:
                    mtl_m = next(m for m in t.getmembers() if m.name.endswith("_MTL.json"))
                    mtl = json.load(t.extractfile(mtl_m))["LANDSAT_METADATA_FILE"]
                    members = {m.name: (m.offset_data, m.size) for m in t.getmembers()}
                pid = mtl["PRODUCT_CONTENTS"]["LANDSAT_PRODUCT_ID"]
                item = contract.collection_item(data_root, lid, None, pid)
                props = item["properties"]
                assert props["acquisition_date"] == mtl["IMAGE_ATTRIBUTES"]["DATE_ACQUIRED"]
                assert props["platform"] == mtl["IMAGE_ATTRIBUTES"]["SPACECRAFT_ID"]
                assert props["cloud_cover"] == float(mtl["IMAGE_ATTRIBUTES"]["CLOUD_COVER"])
                st = mtl["LEVEL2_SURFACE_TEMPERATURE_PARAMETERS"]
                b10 = next(x for x in item["assets"] if x.get("band_code") == "ST_B10")
                assert b10["scale"] == float(st["TEMPERATURE_MULT_BAND_ST_B10"]) and b10["add_offset"] == float(st["TEMPERATURE_ADD_BAND_ST_B10"])
                assert b10["raster"]["bands"][0]["nodata"] == 0 and b10["raster"]["crs"]["epsg"] == 32635 and b10["raster"]["resolution"] == [30.0, 30.0]
                for x in item["assets"]:
                    assert members[x["member"]] == (x["offset"], x["bytes"]), x["member"]
                ctx.setdefault("b10", []).append((lid, pid, b10, a))
                checked += 1
            ctx["lid"] = lid
            return (f"katalog eşitlendi; {checked} sahne MTL ile birebir (tarih, uydu, bulut, ST_B10 ölçek/kaydırma kaynak beyanı, "
                    f"nodata 0, EPSG:32635, 30 m) ve tüm üye konumları tarfile ile aynı")

        def range_read():
            from osgeo import gdal

            lid, pid, b10, archive = ctx["b10"][0]
            loc = describe_asset(data_root, lid, None, pid, "ST_B10")["location"]
            assert loc["range_read"] == "vsisubfile_over_vsis3", loc
            local_path = f"/vsisubfile/{b10['offset']}_{b10['bytes']},{ctx['pkg'].path(archive).as_posix()}"
            gdal.SetConfigOption("CPL_VSIL_NETWORK_STATS_ENABLED", "YES")
            gdal.NetworkStatsReset()
            with gdal_vsis3_session(cloud.bucket, cloud.endpoint, load_credential(cloud.credential_profile, "publisher"), region=cloud.region):
                remote = read_window(loc["gdal_path"], 1, 3000, 3000, 512, 512)
                ds_r = gdal.Open(loc["gdal_path"])
                ov = ds_r.GetRasterBand(1).GetOverview(3).ReadRaster()
                ds_r = None
            stats = json.loads(gdal.NetworkStatsGetAsSerializedJSON() or "{}")
            gdal.SetConfigOption("CPL_VSIL_NETWORK_STATS_ENABLED", None)
            ds_l = gdal.Open(local_path)
            ov_local = ds_l.GetRasterBand(1).GetOverview(3).ReadRaster()
            ds_l = None
            assert remote == read_window(local_path, 1, 3000, 3000, 512, 512) and ov == ov_local
            def _sum(node) -> int:  # GDAL ağ istatistiği iç içe (işleyici/dosya/yöntem); en üst toplam yoksa yapraklar toplanır
                if isinstance(node, dict):
                    if isinstance(node.get("downloaded_bytes"), int):
                        return node["downloaded_bytes"]
                    return sum(_sum(v) for v in node.values())
                return 0
            downloaded = _sum(stats["methods"]) if isinstance(stats.get("methods"), dict) else _sum(stats)
            ctx["network_stats"] = stats
            ctx["range_downloaded"] = downloaded
            return (f"R2 /vsisubfile/+/vsis3/: 512×512 pencere ve 3. genel bakış kaynakla bayt bayt aynı (ham DN, dönüşüm yok); "
                    f"indirilen {downloaded / 1e6:.2f} MB / üye {b10['bytes'] / 1e6:.0f} MB / arşiv {ctx['pkg'].path(archive).stat().st_size / 1e6:.0f} MB")

        def member_materialization():
            lid, pid, b10, archive = ctx["b10"][0]
            cache = ObjectCache(tmp / "cache", 2 * 1024 ** 3, protected_roots=[data_root])
            store.range_bytes = 0
            m = materialize_member(data_root, store, cache, lid, None, pid, "ST_B10")
            got = hashlib.sha256(Path(m["path"]).read_bytes()).hexdigest()
            assert got == b10["sha256"] and m["bytes"] == b10["bytes"], m
            assert store.range_bytes == b10["bytes"], (store.range_bytes, b10["bytes"])
            local_path = f"/vsisubfile/{b10['offset']}_{b10['bytes']},{ctx['pkg'].path(archive).as_posix()}"
            assert read_window(m["path"], 1, 100, 100, 256, 256) == read_window(local_path, 1, 100, 100, 256, 256)
            return (f"yalnız üye indirildi: {store.range_bytes / 1e6:.1f} MB ({store.range_requests} aralık isteği; arşiv "
                    f"{ctx['pkg'].path(archive).stat().st_size / 1e6:.0f} MB indirilmedi); üye SHA-256 kayıtla aynı; değerler ham")

        def bounded_disk():
            work = tree_bytes(work_root)
            cache = tree_bytes(tmp / "cache")
            mirror = tree_bytes(data_root)
            largest = max((p.stat().st_size for p in tmp.rglob("*") if p.is_file()), default=0)
            total_archives = sum(ctx["pkg"].path(a).stat().st_size for a in archives)
            assert largest < min(total_archives, 200 * 1024 ** 2), largest
            assert work < 50 * 1024 ** 2, work
            return (f"yerel yazım: çalışma {work / 1e6:.2f} MB · katalog yansısı {mirror / 1e6:.2f} MB · önbellek (tek üye) "
                    f"{cache / 1e6:.1f} MB · en büyük yerel dosya {largest / 1e6:.1f} MB; arşivler ({total_archives / 1e9:.2f} GB) "
                    f"kopyalanmadı/çıkarılmadı; disk boşluğu farkı {(free_before - shutil.disk_usage(tmp).free) / 1e6:.0f} MB")

        def source_untouched():
            after = {e.path: (original.path(e.path).stat().st_size, original.path(e.path).stat().st_mtime_ns) for e in original.entries}
            assert after == before
            return f"kaynak paket ({len(before)} dosya) boyut ve değişiklik zamanı aynı"

        for name, fn in (("subset_without_copy", subset), ("plan_index_verify", plan),
                         ("interrupt_and_resume_publication", interrupted_then_resumed), ("idempotent_rerun", idempotent),
                         ("commit_matches_manifest", verified), ("catalog_and_metadata_accuracy", catalog_and_metadata),
                         ("remote_range_read", range_read), ("member_materialization", member_materialization),
                         ("bounded_local_disk", bounded_disk), ("source_untouched", source_untouched)):
            if not step(name, fn) and name in ("subset_without_copy", "plan_index_verify", "interrupt_and_resume_publication"):
                break
        os.environ.pop("CPL_VSIL_NETWORK_STATS_ENABLED", None)
    steps.append(Step("namespace", "INFO", namespace))
    return steps