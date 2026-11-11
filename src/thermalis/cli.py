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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="thermalis", description="THERMALIS — Urban Climate Intelligence Framework")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="Programı başlat / start the application")
    start.add_argument("--port", type=int, default=DEFAULT_PORT)
    start.add_argument("--no-browser", action="store_true", help="Tarayıcıyı açma / do not open the browser")
    start.add_argument("--reload", action="store_true", help="Geliştirme: kod değişince yeniden yükle")
    start.set_defaults(func=_start)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
