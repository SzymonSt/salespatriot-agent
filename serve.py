#!/usr/bin/env python3
"""Serve the web UI with clean URLs.

  /                    overview          /supplier/<id>   supplier page
  /suppliers, /buyers  lists             /buyer/<id>      buyer page
  /signin, /workspace  demo sign-in and the signed-in company's workspace
  /data/...            data/ folder      anything else    file from web/
"""

from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / "data"
APP_ROUTES = ("/", "/suppliers", "/buyers", "/signin", "/workspace")
APP_PREFIXES = ("/supplier/", "/buyer/")


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path: str) -> str:
        path = unquote(urlparse(path).path)
        if path in APP_ROUTES or path.startswith(APP_PREFIXES):
            return str(WEB / "index.html")
        base, rel = (DATA, path[len("/data/"):]) if path.startswith("/data/") else (WEB, path.lstrip("/"))
        target = (base / rel).resolve()
        # Never serve anything outside the two folders.
        return str(target) if target.is_relative_to(base) else str(WEB / "__missing__")

    def end_headers(self) -> None:
        # Data and code change while we work on them; always serve the latest.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the SalesPatriot web UI.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), partial(Handler, directory=str(WEB)))
    print(f"Serving on http://{args.host}:{args.port}/")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
