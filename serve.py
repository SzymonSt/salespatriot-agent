#!/usr/bin/env python3
"""Serve the web UI with clean URLs.

  /                    overview          /supplier/<id>   supplier page
  /suppliers, /buyers  lists             /buyer/<id>      buyer page
  /signin, /workspace  demo sign-in and the signed-in company's workspace
  /data/...            data/ folder      anything else    file from web/

Live requests (demo): a buyer posts a requirement, matched suppliers see it pop up.
  POST /api/offers                    {buyer_id, title, cpv_codes, quantity, unit, budget, currency,
                                        need_by, matches: [{supplier_id, item_product}]} → {id}
  GET  /api/offers?supplier_id=<id>   requests sent to that supplier, with its D1-D14 deal check
  GET  /api/offers?buyer_id=<id>      requests posted by that buyer, with every supplier's check
Requests are kept in data/demo/live_requests.json. Deal checks run in a background thread
(deal_due_diligence.run_deal_check); the result appears once it is ready.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
DATA = ROOT / "data"
REQUESTS_FILE = DATA / "demo" / "live_requests.json"
APP_ROUTES = ("/", "/suppliers", "/buyers", "/signin", "/workspace")
APP_PREFIXES = ("/supplier/", "/buyer/")


# --------------------------------------------------------------------------- live requests


class LiveRequests:
    """Buyer requirements and the deal checks for each matched supplier."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.items: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        self.engine = None
        self.engine_error = ""
        threading.Thread(target=self._warm_up, daemon=True).start()

    def _warm_up(self) -> None:
        """Load sanctions lists, TED history and profiles once (~20 s) so checks are fast later."""
        try:
            sys.path.insert(0, str(ROOT))
            import deal_due_diligence

            deal_due_diligence.load_context(DATA)
            self.engine = deal_due_diligence
            print("Deal checks ready", flush=True)
        except Exception as error:  # the UI still works without checks
            self.engine_error = f"{type(error).__name__}: {error}"
            print(f"Deal checks unavailable: {self.engine_error}", flush=True)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    def add(self, body: dict[str, Any]) -> dict[str, Any]:
        matches = [m for m in body.get("matches") or [] if m.get("supplier_id")]
        request = {
            "id": f"REQ-{int(time.time() * 1000)}",
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "buyer_id": body.get("buyer_id", ""),
            "title": (body.get("title") or "").strip()[:300],
            "cpv_codes": body.get("cpv_codes") or [],
            "quantity": body.get("quantity"),
            "unit": body.get("unit") or "",
            "budget": body.get("budget"),
            "currency": body.get("currency") or "EUR",
            "need_by": body.get("need_by") or "",
            "matches": [{"supplier_id": m["supplier_id"], "item_product": m.get("item_product", ""), "check": None} for m in matches],
        }
        with self.lock:
            self.items.append(request)
            self._save()
        threading.Thread(target=self._check, args=(request["id"],), daemon=True).start()
        return request

    def _check(self, request_id: str) -> None:
        # Wait for the warm-up instead of failing the first request after a restart.
        for _ in range(120):
            if self.engine or self.engine_error:
                break
            time.sleep(0.5)
        request = next(r for r in self.items if r["id"] == request_id)
        buyer = self._buyer(request["buyer_id"])
        for match in request["matches"]:
            if not self.engine:
                result = {"error": self.engine_error or "deal checks still loading"}
            else:
                tender = self.engine.tender_from_request(request, buyer)
                result = self.engine.run_deal_check({"tender": tender, "supplier_id": match["supplier_id"]})
            with self.lock:
                match["check"] = compact(result)
                self._save()

    @staticmethod
    def _buyer(buyer_id: str) -> dict[str, Any] | None:
        buyers = json.loads((DATA / "profiles" / "buyers.json").read_text(encoding="utf-8"))
        return next((b for b in buyers if b["id"] == buyer_id), None)

    def for_supplier(self, supplier_id: str) -> list[dict[str, Any]]:
        with self.lock:
            out = []
            for request in self.items:
                match = next((m for m in request["matches"] if m["supplier_id"] == supplier_id), None)
                if match:
                    out.append({**{k: v for k, v in request.items() if k != "matches"}, "match": match})
            return out

    def for_buyer(self, buyer_id: str) -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.items if r["buyer_id"] == buyer_id]


def compact(result: dict[str, Any]) -> dict[str, Any]:
    if "error" in result:
        return {"error": result["error"]}
    return {
        "decision": result["decision"],
        "blocking": result["blocking"],
        "conditions": result["conditions"],
        "checks": [{k: c[k] for k in ("code", "title", "status", "summary", "evidence", "conditions")} for c in result["checks"]],
    }


# --------------------------------------------------------------------------- HTTP


class Handler(SimpleHTTPRequestHandler):
    live: LiveRequests

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

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path != "/api/offers":
            return super().do_GET()
        query = parse_qs(url.query)
        if "supplier_id" in query:
            return self._json(200, self.live.for_supplier(query["supplier_id"][0]))
        if "buyer_id" in query:
            return self._json(200, self.live.for_buyer(query["buyer_id"][0]))
        with self.live.lock:
            return self._json(200, self.live.items)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/offers":
            return self._json(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self._json(400, {"error": "invalid JSON"})
        if not body.get("buyer_id") or not body.get("title"):
            return self._json(400, {"error": "buyer_id and title are required"})
        self._json(201, self.live.add(body))

    def log_message(self, format: str, *args: Any) -> None:
        # Polling every 2 s would flood the terminal; log everything except those GETs.
        if "/api/offers?" not in (args[0] if args else ""):
            super().log_message(format, *args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the SalesPatriot web UI.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    Handler.live = LiveRequests(REQUESTS_FILE)
    server = ThreadingHTTPServer((args.host, args.port), partial(Handler, directory=str(WEB)))
    print(f"Serving on http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
