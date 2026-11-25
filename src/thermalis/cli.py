"""Komut satırı: ``thermalis start``."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"  # güvenlik: yalnız bu bilgisayardan erişilir
DEFAULT_PORT = 8765


def ui_dir() -> Path:
    override = os.environ.get("THERMALIS_UI_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        return sock.connect_ex((HOST, port)) != 0


def _start(args: argparse.Namespace) -> int:
    import uvicorn

    static = ui_dir()
    if not (static / "index.html").is_file():
        print("Arayüz derlenmemiş / UI is not built. Çalıştırın / run:  pixi run build-ui", file=sys.stderr)
        if not args.reload:
            return 1

    if not _port_free(args.port):
        print(f"Port {args.port} kullanımda / in use. Farklı port: thermalis start --port 8766", file=sys.stderr)
        return 1

    url = f"http://{HOST}:{args.port}"
    print(f"THERMALIS çalışıyor / running: {url}  (durdurmak / stop: Ctrl+C)")
    # Anlamsal doğrulama yetkili arayüz oturumu gerektirir; tek kullanımlık kod yalnız bu süreçte geçerlidir.
    from .app import issue_ui_code

    session_url = f"{url}/#auth={issue_ui_code()}"
    if args.no_browser or args.reload:
        print(f"Doğrulama yetkili oturum / authorized session (tek kullanımlık / one-time): {session_url}")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(session_url)).start()

    os.environ["THERMALIS_UI_DIR"] = str(static)
    uvicorn.run(
        "thermalis.cli:build_app",
        factory=True,
        host=HOST,
        port=args.port,
        reload=args.reload,
        log_level="warning",
    )
    return 0


def build_app():
    from .app import create_app

    return create_app(ui_dir())


def _data_root():
    from .settings import effective_data_root, load_settings

    return effective_data_root(load_settings())


def _print_checks(checks) -> None:
    for c in checks:
        print(f"  [{c.status:4}] {c.id:7} {c.title} — {c.detail}")


def _data_register(args: argparse.Namespace) -> int:
    from .data.register import register_package

    root = _data_root()
    print(f"Veri kökü / data root: {root}")
    outcome = register_package(Path(args.package), root, expect_status=args.expect_status)
    _print_checks(outcome.checks)
    print(f"SONUÇ / RESULT: {outcome.status}  {outcome.dataset_id or ''} {outcome.version or ''}")
    print(f"Konum / location: {outcome.location}")
    print(f"Rapor / report: {outcome.report_path}")
    return 0 if outcome.status == "PASS" else 2


def _data_verify(args: argparse.Namespace) -> int:
    from .data.register import verify_registered

    checks = verify_registered(_data_root(), args.dataset_id, args.version)
    _print_checks(checks)
    ok = all(c.status == "PASS" for c in checks)
    print("SONUÇ / RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


def _data_list(args: argparse.Namespace) -> int:
    from .data.catalog import list_versions

    versions = list_versions(_data_root())
    if not versions:
        print("Kayıtlı veri yok / no registered data")
    for v in versions:
        layers = ", ".join(f"{l['name']}={l['feature_count']}" for l in v["layers"] if l["role"] == "core")
        print(f"{v['dataset_id']}  {v['version']}  {v['quality_status']}  [{layers}]")
    return 0


def _incoming_dir(args: argparse.Namespace) -> Path | None:
    from .settings import load_settings

    value = args.incoming or load_settings().transfer_incoming_dir
    return Path(value) if value else None


def _data_ingest(args: argparse.Namespace) -> int:
    from .data.ingest import IngestLocked, release_stale_lock, run_ingest

    root = _data_root()
    if args.release_stale_lock:
        print("Kilit kaldırıldı / lock released" if release_stale_lock(root) else "Kilit yok / no lock")
        return 0
    incoming = _incoming_dir(args)
    if incoming is None:
        print("Gelen klasör ayarlı değil / incoming folder not set:  thermalis data set-incoming <klasör>", file=sys.stderr)
        return 1
    print(f"Veri kökü / data root: {root}")
    print(f"Gelen klasör / incoming: {incoming}")
    try:
        summary = run_ingest(incoming, root, settle_seconds=args.settle_seconds, dry_run=args.dry_run, only=args.package)
    except IngestLocked as exc:
        print(f"Başka bir entegrasyon çalışıyor / another ingest is running: {exc}", file=sys.stderr)
        print("Çökme sonrası kalan kilit için / stale lock:  thermalis data ingest --release-stale-lock", file=sys.stderr)
        return 3
    for action in summary.recovery:
        print(f"  [RECOVERY] {action['action']}: {action['from']} → {action['to']} ({action['reason']})")
    for c in summary.candidates:
        print(f"  [{c.state:18}] {c.path.name}  {c.dataset_id or ''} {c.version or ''}  {c.reason}")
    for r in summary.results:
        print(f"SONUÇ / RESULT  {r.package_path.name}: teknik={r.technical} kalite={r.quality} denetim={r.audit} erişim={r.access}")
        if r.stop_reason:
            print(f"    durma nedeni / stop: {r.stop_reason}")
        if r.user_decision:
            print(f"    kullanıcı kararı / decision: {r.user_decision}")
        print(f"    rapor / report: {r.folder / 'INGESTION_REPORT.md' if r.folder else '—'}")
    if args.dry_run:
        print("Kuru çalıştırma: hiçbir şey kaydedilmedi / dry run: nothing registered")
    return 0 if summary.ok else 2


def _data_set_incoming(args: argparse.Namespace) -> int:
    from .settings import load_settings, save_settings

    path = Path(args.folder)
    if not path.is_dir():
        print(f"Klasör yok / folder not found: {path}", file=sys.stderr)
        return 1
    settings = load_settings()
    settings.transfer_incoming_dir = str(path)
    save_settings(settings)
    print(f"Gelen klasör / incoming folder: {path}")
    return 0


def _data_reindex(args: argparse.Namespace) -> int:
    from .data.catalog_index import query_record_issues, query_versions, rebuild_index

    root = _data_root()
    print(f"Dizin / index: {rebuild_index(root)}")
    for v in query_versions(root):
        print(f"  {v['dataset_id']}  {v['version']}  {v['quality_status']}  {v['access_class']}")
    for issue in query_record_issues(root):
        print(f"  [KAYIT SORUNU] {issue['dataset_id']} {issue['version']}  {issue['code']}: {issue['detail']}  ({issue['evidence']})")
    return 0


def _data_formats(args: argparse.Namespace) -> int:
    from .data.adapters import support_matrix

    print("Desteklenen veri türleri / supported formats (diğer her tür BLOCKED + SUPPORT_GAP.json):")
    for a in support_matrix():
        print(f"  {a['key']:16} {a['label']}  ({', '.join(a['suffixes'])}) · doğrulandı: {a['validated_with']}")
        for check in a["checks"]:
            print(f"  {'':16} - {check}")
    return 0


def _data_layers(args: argparse.Namespace) -> int:
    from .data.catalog_index import query_layers

    for l in query_layers(_data_root(), args.dataset_id, args.version):
        print(f"{l['layer_dataset_id']}  {l['version']}  {l['role']:9} {l['geometry_type']:14} {l['feature_count']:>7}  asset={l['source_asset_id']}")
    return 0


def _data_findings(args: argparse.Namespace) -> int:
    from .data.catalog_index import finding_summary, query_findings

    root = _data_root()
    print(json.dumps(finding_summary(root, args.dataset_id, args.version), ensure_ascii=False, indent=2))
    if args.list:
        for f in query_findings(root, args.dataset_id, args.version, category=args.category):
            print(f"  #{f['seq']:<3} {f['exception_id']:22} uniq={f['id_unique']} {f['origin']:28} {f['kind']:20} {f['category']}")
    return 0


def _storage_status(args: argparse.Namespace) -> int:
    from .data.catalog import release_store
    from .objectstore.credentials import credential_status
    from .objectstore.errors import StorageError
    from .objectstore.factory import object_cache
    from .settings import effective_data_root, load_settings

    settings = load_settings()
    store = release_store(effective_data_root(settings))
    print(f"Etkin depo / active backend: {settings.storage_backend}  (kök / root: {store.root})")
    print("Yetenekler / capabilities:", json.dumps(store.capabilities.as_dict()))
    cloud = settings.cloud
    configured = bool(cloud.endpoint and cloud.bucket)
    print(f"Bulut / cloud ({cloud.provider}): {'yapılandırıldı / configured' if configured else 'yapılandırılmadı / not configured'}"
          + (f"  bucket={cloud.bucket}" if configured else ""))
    try:
        roles = credential_status(cloud.credential_profile)
        print("Kimlik bilgisi / credentials (değer gösterilmez):", ", ".join(f"{r}={'var' if ok else 'yok'}" for r, ok in roles.items()))
    except StorageError as exc:
        print(f"Kimlik deposu / credential store: {exc.code} {exc.detail}")
    try:
        cache = object_cache(settings)
        print(f"Önbellek / cache: {cache.root}  {cache.usage()} / {cache.max_bytes} bayt")
    except StorageError as exc:
        print(f"Önbellek / cache: {exc.code} {exc.detail}")
    return 0


def _storage_configure_cloud(args: argparse.Namespace) -> int:
    from .objectstore.errors import StorageError
    from .objectstore.s3 import validate_endpoint
    from .settings import load_settings, save_settings

    settings = load_settings()
    try:
        endpoint = validate_endpoint(args.endpoint)
    except StorageError:
        print("Geçersiz uç nokta / invalid endpoint: yalnız https://<host> (yol/sorgu yok)", file=sys.stderr)
        return 1
    settings.cloud.endpoint = endpoint
    settings.cloud.bucket = args.bucket
    if args.intake_bucket is not None:
        settings.cloud.intake_bucket = args.intake_bucket or None
    settings.cloud.credential_profile = args.profile
    type(settings.cloud).model_validate(settings.cloud.model_dump())
    save_settings(settings)
    print("Bulut ayarı kaydedildi (gizli değer yok) / cloud settings saved (no secrets). Etkin depo değişmedi: local.")
    return 0


def _storage_credentials(args: argparse.Namespace) -> int:
    import getpass

    from .objectstore.credentials import delete_credential, store_credential
    from .objectstore.errors import StorageError
    from .settings import load_settings

    profile = args.profile or load_settings().cloud.credential_profile
    try:
        if args.action == "clear":
            removed = delete_credential(profile, args.role)
            print(f"{profile}:{args.role} " + ("silindi / removed" if removed else "kayıt yok / not present"))
            return 0
        if not sys.stdin.isatty() and not args.allow_non_tty:
            print("Etkileşimli terminal gerekli / interactive terminal required (değerler yankısız sorulur).", file=sys.stderr)
            return 1
        key_id = getpass.getpass("Access Key ID (gizli giriş / hidden): ").strip()
        secret = getpass.getpass("Secret Access Key (gizli giriş / hidden): ").strip()
        store_credential(profile, args.role, key_id, secret)
    except StorageError as exc:
        print(f"Başarısız / failed: {exc.code} {exc.detail}", file=sys.stderr)
        return 1
    print(f"{profile}:{args.role} işletim sistemi kimlik deposuna kaydedildi / stored in the OS credential store.")
    return 0


def _storage_smoke_test(args: argparse.Namespace) -> int:
    from .objectstore.errors import StorageError
    from .objectstore.factory import cloud_store
    from .objectstore.smoke import LIVE_ENV, run_smoke
    from .settings import load_settings

    if not args.confirm_live or os.environ.get(LIVE_ENV) != "1":
        print(f"Canlı test kapalı / live test disabled: hem --confirm-live hem {LIVE_ENV}=1 gerekir.", file=sys.stderr)
        return 1
    try:
        store = cloud_store(load_settings(), args.role)
        steps = run_smoke(store)
    except StorageError as exc:
        print(f"Başlatılamadı / could not start: {exc.code} {exc.detail}", file=sys.stderr)
        return 1
    for s in steps:
        print(f"  [{s.status}] {s.name} — {s.detail}")
    ok = all(s.status == "PASS" for s in steps)
    print("SONUÇ / RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


def _storage_intake_e2e(args: argparse.Namespace) -> int:
    from .cloud.e2e import run_e2e
    from .objectstore.errors import StorageError
    from .objectstore.factory import cloud_store
    from .objectstore.smoke import LIVE_ENV
    from .settings import load_settings

    if not args.confirm_live or os.environ.get(LIVE_ENV) != "1":
        print(f"Canlı test kapalı / live test disabled: hem --confirm-live hem {LIVE_ENV}=1 gerekir.", file=sys.stderr)
        return 1
    settings = load_settings()
    try:
        store = cloud_store(settings, args.role)
        steps = run_e2e(store, provider=settings.cloud.provider, bucket=settings.cloud.bucket or "")
    except StorageError as exc:
        print(f"Başlatılamadı / could not start: {exc.code} {exc.detail}", file=sys.stderr)
        return 1
    for s in steps:
        print(f"  [{s.status}] {s.name} — {s.detail}")
    ok = all(s.status in ("PASS", "INFO") for s in steps)
    print("SONUÇ / RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


def _storage_cloud_scan(args: argparse.Namespace) -> int:
    """Bulut kabulünü bir kez elle çalıştırır (uygulama kapalıyken de). Ayarlarda bulut kabulü açık olmalı."""
    from .cloud.intake import cloud_intake_from_settings
    from .objectstore.errors import StorageError
    from .settings import effective_data_root, load_settings

    settings = load_settings()
    if not settings.cloud_intake.enabled:
        print("Bulut kabulü kapalı / cloud intake disabled (settings.cloud_intake.enabled)", file=sys.stderr)
        return 1
    try:
        result = cloud_intake_from_settings(settings, effective_data_root(settings)).scan()
    except StorageError as exc:
        print(f"Başarısız / failed: {exc.code} {exc.detail}", file=sys.stderr)
        return 3
    print(f"Tarama / scan: {result.status} · duyuru {result.discovered}" + (f" · hata {result.error}" if result.error else ""))
    for o in result.outcomes:
        print(f"  [{o.status}] {o.package_id} {o.dataset_id or ''} {o.version or ''} {o.code}")
    if result.sync:
        print(f"Katalog / catalog: {result.sync['status']} · yansıtılan {len(result.sync['mirrored'])} · değişmeyen {result.sync['unchanged']}")
    return 0 if result.status in ("ok", "busy") else 3


def _release_store_from_settings(settings):
    from .objectstore.factory import cloud_store
    from .objectstore.prefixed import namespaced

    return namespaced(cloud_store(settings, "publisher"), settings.cloud.namespace)


def _storage_migrate_release(args: argparse.Namespace) -> int:
    """Yerel kayıtlı sürümü buluta taşır. Varsayılan: kuru çalıştırma (ağ yok). Gerçek yükleme için --execute."""
    from .cloud.migrate import MigrationRefused, migrate_release, plan_migration, verify_migration
    from .objectstore.errors import StorageError
    from .settings import effective_data_root, load_settings

    settings = load_settings()
    try:
        plan = plan_migration(effective_data_root(settings), args.dataset_id, args.version, provider=settings.cloud.provider,
                              bucket=settings.cloud.bucket or "", authorization_path=Path(args.authorization) if args.authorization else None)
    except MigrationRefused as exc:
        print(f"REDDEDİLDİ / REFUSED: {exc.code} {exc.detail}", file=sys.stderr)
        return 2
    print(f"Plan: {plan.dataset_id} {plan.version} · paket {plan.package_id} · erişim {plan.access_class} (değişmez)")
    print(f"  {len(plan.files)} veri dosyası · {len(plan.records)} kayıt · {plan.total_bytes} bayt · işlem {plan.operation_id}")
    print(f"  yerel doğrulama: {plan.local_verification['status']} ({plan.local_verification['checked_files']} dosya, tam SHA-256)")
    for name in sorted(plan.records):
        print(f"    records/{name}")
    for w in plan.warnings:
        print(f"  UYARI / WARNING: {w}")
    if not args.execute:
        print("Kuru çalıştırma: hiçbir şey yüklenmedi / dry run: nothing uploaded (gerçek taşıma için --execute)")
        return 0
    try:
        store = _release_store_from_settings(settings)
        result = migrate_release(store, plan)
        check = verify_migration(store, plan)
    except MigrationRefused as exc:
        print(f"REDDEDİLDİ / REFUSED: {exc.code} {exc.detail}", file=sys.stderr)
        return 2
    except StorageError as exc:
        print(f"BAŞARISIZ / FAILED: {exc.code} {exc.detail} — aynı komutla sürdürülebilir", file=sys.stderr)
        return 3
    print(f"SONUÇ / RESULT: {result['status']} {result['release_prefix']} · doğrulama {check['status']}")
    return 0 if check["status"] == "PASS" else 2


def _storage_compare_release(args: argparse.Namespace) -> int:
    from .cloud.compare import compare_release
    from .objectstore.errors import StorageError
    from .settings import effective_data_root, load_settings

    settings = load_settings()
    try:
        report = compare_release(effective_data_root(settings), _release_store_from_settings(settings), args.dataset_id, args.version,
                                 provider=settings.cloud.provider, bucket=settings.cloud.bucket or "", namespace=settings.cloud.namespace)
    except StorageError as exc:
        print(f"BAŞARISIZ / FAILED: {exc.code} {exc.detail}", file=sys.stderr)
        return 3
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 2


def _storage_migration_rehearsal(args: argparse.Namespace) -> int:
    from .cloud.e2e import run_migration_rehearsal
    from .objectstore.errors import StorageError
    from .objectstore.factory import cloud_store
    from .objectstore.smoke import LIVE_ENV
    from .settings import load_settings

    if not args.confirm_live or os.environ.get(LIVE_ENV) != "1":
        print(f"Canlı test kapalı / live test disabled: hem --confirm-live hem {LIVE_ENV}=1 gerekir.", file=sys.stderr)
        return 1
    settings = load_settings()
    try:
        steps = run_migration_rehearsal(cloud_store(settings, "publisher"), provider=settings.cloud.provider,
                                        bucket=settings.cloud.bucket or "")
    except StorageError as exc:
        print(f"Başlatılamadı / could not start: {exc.code} {exc.detail}", file=sys.stderr)
        return 1
    for s in steps:
        print(f"  [{s.status}] {s.name} — {s.detail}")
    ok = all(s.status in ("PASS", "INFO") for s in steps)
    print("SONUÇ / RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 2


def _storage_cloud_intake_toggle(args: argparse.Namespace) -> int:
    from .settings import load_settings, save_settings

    settings = load_settings()
    settings.cloud_intake.enabled = args.state == "on"
    if args.interval is not None:
        settings.cloud_intake.interval_seconds = args.interval
    type(settings.cloud_intake).model_validate(settings.cloud_intake.model_dump())
    save_settings(settings)
    print(f"Bulut kabulü / cloud intake: {args.state} · aralık {settings.cloud_intake.interval_seconds} s · "
          f"etkin depo / active backend: {settings.storage_backend} (değişmedi)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="thermalis", description="THERMALIS — Urban Climate Intelligence Framework")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="Programı başlat / start the application")
    start.add_argument("--port", type=int, default=DEFAULT_PORT)
    start.add_argument("--no-browser", action="store_true", help="Tarayıcıyı açma / do not open the browser")
    start.add_argument("--reload", action="store_true", help="Geliştirme: kod değişince yeniden yükle")
    start.set_defaults(func=_start)

    data = sub.add_parser("data", help="THERMALIS DATA kataloğu / catalog")
    data_sub = data.add_subparsers(dest="data_command", required=True)
    reg = data_sub.add_parser("register", help="Aktarım paketini kaydet / register a transfer package")
    reg.add_argument("package", help="Paket kök klasörü (içinde PACKAGE/ bulunan)")
    reg.add_argument("--expect-status", default=None, help="Beklenen kalite durumu, örn. QA_CONDITIONAL")
    reg.set_defaults(func=_data_register)
    ver = data_sub.add_parser("verify", help="Kayıtlı sürümü yeniden doğrula / re-verify a registered version")
    ver.add_argument("dataset_id")
    ver.add_argument("version")
    ver.set_defaults(func=_data_verify)
    lst = data_sub.add_parser("list", help="Kayıtlı veriler / registered data")
    lst.set_defaults(func=_data_list)

    ing = data_sub.add_parser("ingest", help="Gelen klasördeki paketleri otomatik işle / ingest incoming packages")
    ing.add_argument("--incoming", default=None, help="Gelen paket klasörü (varsayılan: ayarlardaki transfer_incoming_dir)")
    ing.add_argument("--package", default=None, help="Yalnız bu alt klasörü işle")
    ing.add_argument("--settle-seconds", type=float, default=120.0, help="Klasör bu kadar süre değişmeden kalmalı (kopyalama bitti)")
    ing.add_argument("--dry-run", action="store_true", help="Yalnız keşif: hiçbir şey kaydetme")
    ing.add_argument("--release-stale-lock", action="store_true", help="Çökme sonrası kalan kilidi kaldır")
    ing.set_defaults(func=_data_ingest)
    inc = data_sub.add_parser("set-incoming", help="Gelen paket klasörünü ayarla / set incoming folder")
    inc.add_argument("folder")
    inc.set_defaults(func=_data_set_incoming)
    rix = data_sub.add_parser("reindex", help="Sorgulanabilir katalog dizinini kayıtlardan yeniden üret")
    rix.set_defaults(func=_data_reindex)
    fmt = data_sub.add_parser("formats", help="Format adaptörlerinin destek durumu")
    fmt.set_defaults(func=_data_formats)
    lay = data_sub.add_parser("layers", help="Katmanlar (bağımsız veri setleri olarak)")
    lay.add_argument("dataset_id", nargs="?")
    lay.add_argument("version", nargs="?")
    lay.set_defaults(func=_data_layers)
    fnd = data_sub.add_parser("findings", help="Kalite kayıtları özeti")
    fnd.add_argument("dataset_id")
    fnd.add_argument("version")
    fnd.add_argument("--category", default=None)
    fnd.add_argument("--list", action="store_true")
    fnd.set_defaults(func=_data_findings)

    storage = sub.add_parser("storage", help="Nesne deposu ve bulut ayarları / object storage")
    storage_sub = storage.add_subparsers(dest="storage_command", required=True)
    st = storage_sub.add_parser("status", help="Depo, önbellek ve kimlik bilgisi durumu (gizli değer gösterilmez)")
    st.set_defaults(func=_storage_status)
    cc = storage_sub.add_parser("configure-cloud", help="R2 uç noktası ve kovası (gizli değer değil); etkin depo değişmez")
    cc.add_argument("--endpoint", required=True, help="https://<hesap>.r2.cloudflarestorage.com")
    cc.add_argument("--bucket", required=True)
    cc.add_argument("--intake-bucket", default=None, help="İsteğe bağlı ayrı kabul kovası (ikinci bilgisayarın yazdığı)")
    cc.add_argument("--profile", default="default")
    cc.set_defaults(func=_storage_configure_cloud)
    cr = storage_sub.add_parser("credentials", help="Kimlik bilgisini OS kimlik deposuna yaz / sil (değerler yankısız sorulur)")
    cr.add_argument("action", choices=["set", "clear"])
    cr.add_argument("--role", required=True, choices=["uploader", "publisher"])
    cr.add_argument("--profile", default=None)
    cr.add_argument("--allow-non-tty", action="store_true", help=argparse.SUPPRESS)
    cr.set_defaults(func=_storage_credentials)
    sm = storage_sub.add_parser("smoke-test", help="CANLI R2 duman testi (sentetik veri; açık onay gerekir)")
    sm.add_argument("--role", default="publisher", choices=["uploader", "publisher"])
    sm.add_argument("--confirm-live", action="store_true")
    sm.set_defaults(func=_storage_smoke_test)
    e2e = storage_sub.add_parser("intake-e2e", help="CANLI uçtan uca bulut kabul testi (sentetik; yalıtılmış ad alanı)")
    e2e.add_argument("--role", default="publisher", choices=["uploader", "publisher"])
    e2e.add_argument("--confirm-live", action="store_true")
    e2e.set_defaults(func=_storage_intake_e2e)
    cs = storage_sub.add_parser("cloud-scan", help="Bulut kabulünü bir kez çalıştır ve kataloğu eşitle")
    cs.set_defaults(func=_storage_cloud_scan)
    ci = storage_sub.add_parser("cloud-intake", help="Bulut kabulünü aç/kapat (etkin depo DEĞİŞMEZ: local kalır)")
    ci.add_argument("state", choices=["on", "off"])
    ci.add_argument("--interval", type=int, default=None, help="Tarama aralığı (saniye, en az 60)")
    ci.set_defaults(func=_storage_cloud_intake_toggle)
    mr = storage_sub.add_parser("migrate-release", help="Yerel sürümü buluta taşı (varsayılan kuru çalıştırma; --execute ile gerçek)")
    mr.add_argument("dataset_id")
    mr.add_argument("version")
    mr.add_argument("--authorization", default=None, help="RESTRICTED/UNKNOWN için bulut depolama yetki kaydı (JSON)")
    mr.add_argument("--execute", action="store_true", help="Gerçekten yükle (yoksa yalnız doğrula ve planı göster)")
    mr.set_defaults(func=_storage_migrate_release)
    cr2 = storage_sub.add_parser("compare-release", help="Yerel kayıt ile bulut kopyasını bilimsel olarak karşılaştır")
    cr2.add_argument("dataset_id")
    cr2.add_argument("version")
    cr2.set_defaults(func=_storage_compare_release)
    rh = storage_sub.add_parser("migration-rehearsal", help="CANLI taşıma provası (yalnız sentetik; yalıtılmış ad alanı)")
    rh.add_argument("--confirm-live", action="store_true")
    rh.set_defaults(func=_storage_migration_rehearsal)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
