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
