"""Komut satırı: ``thermalis start``."""

from __future__ import annotations

import argparse
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

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
