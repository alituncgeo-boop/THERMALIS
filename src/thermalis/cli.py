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
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
