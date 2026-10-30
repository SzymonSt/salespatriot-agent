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

Company agents (Gemini, app/backend): each new request is also sent to the backend
(POST /v1/agents/offers), where every matched supplier's agent assesses it and, if it can supply,
starts a conversation with the buyer's agent. /v1/* is proxied to the backend so the browser
stays on one origin. Start the backend separately:
  uv run --project app/backend uvicorn app.backend.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
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
BACKEND = "http://127.0.0.1:8000"  # FastAPI agents backend; --backend overrides


def backend_call(method: str, path: str, body: bytes | None = None) -> tuple[int, bytes]:
    """Forward one request to the agents backend; (status, JSON body)."""
    request = urllib.request.Request(f"{BACKEND}{path}", data=body, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except (urllib.error.URLError, OSError) as error:
        detail = f"agents backend not reachable at {BACKEND}: {getattr(error, 'reason', error)}"
        return 503, json.dumps({"detail": detail}).encode("utf-8")


# --------------------------------------------------------------------------- live requests


class LiveRequests:
    """Buyer requirements and the deal checks for each matched supplier."""

    auto_agents = True  # every matched supplier agent starts after its deal check; --manual-agents waits for a click

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

    def _start_agent(self, request: dict[str, Any], match: dict[str, Any]) -> None:
        """Hand the request and this supplier's D1-D14 result to its agent (backend)."""
        body = {"request": {**{k: v for k, v in request.items() if k != "matches"}, "deal_check": match["check"]},
                "supplier_ids": [match["supplier_id"]]}
        status, payload = backend_call("POST", "/v1/agents/offers", json.dumps(body).encode("utf-8"))
        with self.lock:
            match["agent"] = "started" if status == 202 else f"not started (HTTP {status}): {payload[:200].decode('utf-8', 'replace')}"
            self._save()

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
            # Each matched supplier's agent starts once its deal check is known. With --manual-agents it waits
            # for the supplier's "Let agent respond" (POST /api/offers/<id>/respond).
            if self.auto_agents:
                threading.Thread(target=self._start_agent, args=(request, match), daemon=True).start()

    def respond(self, request_id: str, supplier_id: str) -> tuple[int, dict[str, Any]]:
        """The supplier clicked "Let agent respond": its agent assesses the request and may open the conversation."""
        with self.lock:
            request = next((r for r in self.items if r["id"] == request_id), None)
            match = next((m for m in (request or {}).get("matches", []) if m["supplier_id"] == supplier_id), None)
            if match is None:
                return 404, {"error": "request not sent to this supplier"}
            if match.get("check") is None:
                return 409, {"error": "deal check still running"}
            if str(match.get("agent", "")).startswith("started"):
                return 200, {"status": "already started"}
            match["agent"] = "starting"
        threading.Thread(target=self._start_agent, args=(request, match), daemon=True).start()
        return 202, {"status": "assessing"}

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
                    # The budget is the buyer's private ceiling: drop it and mask it inside the deal check.
                    public = {k: v for k, v in request.items() if k not in ("matches", "budget")}
                    out.append({**public, "match": redact(match, request.get("budget"))})
            return out

    def for_buyer(self, buyer_id: str) -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.items if r["buyer_id"] == buyer_id]


def redact(value: Any, budget: Any) -> Any:
    """Replace the buyer's budget wherever the deal check quotes it (e.g. "€180,000")."""
    if not budget:
        return value
    text = json.dumps(value, ensure_ascii=False)
    for shown in {f"{float(budget):,.0f}", f"{int(float(budget))}"}:
        text = text.replace(f"€{shown}", "[buyer budget]").replace(shown, "[buyer budget]")
    return json.loads(text)


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

    def _proxy(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        status, body = backend_call(self.command, self.path, self.rfile.read(length) if length else None)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path.startswith("/v1/"):
            return self._proxy()
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
        if urlparse(self.path).path.startswith("/v1/"):
            return self._proxy()
        respond = re.fullmatch(r"/api/offers/([^/]+)/respond", urlparse(self.path).path)
        if respond:
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._json(400, {"error": "invalid JSON"})
            return self._json(*self.live.respond(respond.group(1), body.get("supplier_id", "")))
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
        line = args[0] if args else ""
        if "/api/offers?" not in line and "/v1/conversations?" not in line:
            super().log_message(format, *args)


def main() -> int:
    global BACKEND
    parser = argparse.ArgumentParser(description="Serve the SalesPatriot web UI.")
    parser.add_argument("--port", type=int, default=8765, help="default 8765; the agents backend uses 8000")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--backend", default=BACKEND, help=f"agents backend URL (default {BACKEND})")
    parser.add_argument("--manual-agents", action="store_true",
                        help="start a supplier agent only when the supplier clicks Let agent respond (default: automatic)")
    parser.add_argument("--reset-demo", action="store_true", help="clear posted requests (data/demo/live_requests.json) first")
    args = parser.parse_args()
    BACKEND = args.backend.rstrip("/")
    if args.reset_demo:
        REQUESTS_FILE.unlink(missing_ok=True)
    LiveRequests.auto_agents = not args.manual_agents
    Handler.live = LiveRequests(REQUESTS_FILE)
    server = ThreadingHTTPServer((args.host, args.port), partial(Handler, directory=str(WEB)))
    print(f"Serving on http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
