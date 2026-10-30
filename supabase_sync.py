#!/usr/bin/env python3
"""Upsert supplier/buyer profiles and due-diligence results into Supabase.

Pipeline: ted_company_profiles.py → ted_supplier_contracts.py → supplier_due_diligence.py
→ translate_data.py → supabase_sync.py. The JSON files under data/profiles are the build
output; Supabase is what the app and agents read.

Credentials: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY from the environment or from a
gitignored .env file next to this script. Never commit the service-role key.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BATCH = 200
SUPPLIER_COLUMNS = [
    "id", "name", "legal_name", "country", "city", "website", "identifier", "company_size", "summary",
    "ted_activity", "track_record", "top_cpv", "top_counterparts", "weapons_ammo_categories",
    "capabilities", "offering", "catalogue", "recent_notices",
]
BUYER_COLUMNS = [
    "id", "name", "legal_name", "country", "city", "website", "identifier", "legal_type", "main_activity",
    "summary", "ted_activity", "top_cpv", "top_counterparts", "weapons_ammo_categories", "recent_notices",
]
# Keys stored in their own tables, not in suppliers.extra.
SEPARATE_KEYS = {"contracts", "due_diligence", "kyc", "deal_baseline", "role"}
CATEGORY_COLUMNS = {
    "identity": "identity_score",
    "ownership": "ownership_score",
    "track_record": "track_record_score",
    "financial": "financial_score",
    "defence_credentials": "defence_credentials_score",
    "integrity": "integrity_score",
}


# --------------------------------------------------------------------------- client


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Supabase:
    def __init__(self, url: str, key: str) -> None:
        self.base = url.rstrip("/") + "/rest/v1/"
        self.headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    @classmethod
    def from_env(cls) -> "Supabase | None":
        load_env(Path(__file__).with_name(".env"))
        url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        return cls(url, key) if url and key else None

    def request(self, method: str, path: str, body: Any = None, prefer: str = "") -> Any:
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=120) as response:
                raw = response.read()
        except HTTPError as error:
            detail = error.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"Supabase {method} {path}: HTTP {error.code} {detail}") from None
        return json.loads(raw) if raw else None

    def upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> None:
        for start in range(0, len(rows), BATCH):
            self.request("POST", f"{table}?on_conflict={on_conflict}", rows[start:start + BATCH],
                         prefer="resolution=merge-duplicates,return=minimal")

    def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        for start in range(0, len(rows), BATCH):
            self.request("POST", table, rows[start:start + BATCH], prefer="return=minimal")

    def delete_in(self, table: str, column: str, values: list[str]) -> None:
        for start in range(0, len(values), BATCH):
            chunk = ",".join(f'"{v}"' for v in values[start:start + BATCH])
            self.request("DELETE", f"{table}?{column}=in.({urllib.parse.quote(chunk, safe=',')})")

    def select(self, table: str, query: str) -> list[dict[str, Any]]:
        return self.request("GET", f"{table}?{query}") or []


# --------------------------------------------------------------------------- row mapping


def nullable(value: Any) -> Any:
    return None if value in ("", [], {}) else value


def profile_row(profile: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    row = {column: nullable(profile.get(column)) for column in columns}
    row["name"] = profile.get("name") or profile.get("legal_name") or profile["id"]
    row["extra"] = {k: v for k, v in profile.items() if k not in columns and k not in SEPARATE_KEYS}
    return row


def contract_rows(profile: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for contract in profile.get("contracts") or []:
        value = contract.get("awarded_value") or {}
        received = contract.get("tenders_received")
        rows.append({
            "supplier_id": profile["id"],
            "notice_number": contract.get("notice_number"),
            "lot_id": nullable(contract.get("lot_id")),
            "notice_type": nullable(contract.get("notice_type")),
            "published": nullable(contract.get("published")),
            "notice_title": nullable(contract.get("notice_title")),
            "title": nullable(contract.get("title")),
            "description": nullable(contract.get("description")),
            "contract_title": nullable(contract.get("contract_title")),
            "buyer": nullable(contract.get("buyer")),
            "buyer_country": nullable(contract.get("buyer_country")),
            "cpv_codes": contract.get("cpv_codes") or [],
            "quantity": nullable(contract.get("quantity")),
            "duration": nullable(contract.get("duration")),
            "framework_agreement": contract.get("framework_agreement"),
            "awarded_value": value.get("value"),
            "awarded_currency": nullable(value.get("currency")),
            "tenders_received": int(received) if isinstance(received, (int, float)) else None,
            "co_winners": contract.get("co_winners") or [],
            "subcontracting": nullable(contract.get("subcontracting")),
            "award_date": nullable((contract.get("award_date") or "")[:10]),
            "contract_signed": nullable((contract.get("contract_signed") or "")[:10]),
            "winner_confirmed": contract.get("winner_confirmed"),
            "ted_url": nullable(contract.get("ted_url")),
        })
    return rows


def due_diligence_row(supplier_id: str, dd: dict[str, Any]) -> dict[str, Any]:
    row = {
        "supplier_id": supplier_id,
        "method_version": dd["method_version"],
        "assessed_at": dd["assessed_at"],
        "total_score": dd["total_score"],
        "score_before_caps": dd.get("score_before_caps"),
        "verdict": dd["verdict"],
        "confidence": dd["confidence"],
        "data_coverage": dd.get("data_coverage"),
        "red_flags": dd.get("red_flags") or [],
        "caps_applied": dd.get("caps_applied") or [],
        "gates": dd.get("gates") or [],
        "categories": dd["categories"],
        "supplier_documents": dd.get("supplier_documents", 0),
        "sanctions_lists_loaded": dd.get("sanctions_lists_loaded"),
        "disclaimer": dd.get("disclaimer"),
    }
    for key, column in CATEGORY_COLUMNS.items():
        row[column] = (dd["categories"].get(key) or {}).get("score")
    return row


def kyc_rows(supplier_id: str, kyc: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summary = {
        "supplier_id": supplier_id,
        "kyc_status": kyc["kyc_status"],
        "kyc_checked_at": kyc["kyc_checked_at"],
        "refresh_due": kyc["refresh_due"],
        "open_items": kyc.get("open_items") or [],
    }
    checks = [
        {
            "supplier_id": supplier_id,
            "check_code": code,
            "title": check["title"],
            "gate": check["gate"],
            "status": check["status"],
            "summary": check.get("summary"),
            "sources": [s for s in check.get("sources") or [] if s],
            "detail": check.get("detail"),
            "checked_at": kyc["kyc_checked_at"],
        }
        for code, check in kyc["checks"].items()
    ]
    return summary, checks


def baseline_row(supplier_id: str, baseline: dict[str, Any]) -> dict[str, Any]:
    capacity = baseline.get("D6_capacity_hints") or {}
    finance = baseline.get("D10_financial_capacity") or {}
    integrity = baseline.get("D12_integrity_signals") or {}
    return {
        "supplier_id": supplier_id,
        "largest_lot_eur": capacity.get("largest_lot_eur"),
        "revenue_eur": finance.get("revenue_eur"),
        "revenue_year": finance.get("revenue_year"),
        "single_bid_share": integrity.get("single_bid_share"),
        "trader_not_manufacturer": integrity.get("registered_as_trader_not_manufacturer"),
        "related_supplier_ids": integrity.get("related_suppliers_same_owner") or [],
        "baseline": baseline,
    }


# --------------------------------------------------------------------------- sync


def sync_suppliers(db: Supabase, profiles: list[dict[str, Any]], due_diligence_only: bool = False) -> None:
    ids = [p["id"] for p in profiles]
    if not due_diligence_only:
        db.upsert("suppliers", [profile_row(p, SUPPLIER_COLUMNS) for p in profiles], "id")
        db.delete_in("supplier_contracts", "supplier_id", ids)
        db.insert("supplier_contracts", [row for p in profiles for row in contract_rows(p)])
    dd_rows = [due_diligence_row(p["id"], p["due_diligence"]) for p in profiles if p.get("due_diligence")]
    db.upsert("supplier_due_diligence", dd_rows, "supplier_id")
    kyc_summaries, kyc_checks = [], []
    for p in profiles:
        if p.get("kyc"):
            summary, checks = kyc_rows(p["id"], p["kyc"])
            kyc_summaries.append(summary)
            kyc_checks.extend(checks)
    db.upsert("supplier_kyc", kyc_summaries, "supplier_id")
    db.upsert("supplier_kyc_checks", kyc_checks, "supplier_id,check_code")
    db.upsert("supplier_deal_baseline", [baseline_row(p["id"], p["deal_baseline"]) for p in profiles if p.get("deal_baseline")], "supplier_id")
    print(f"suppliers: {len(profiles)} profiles, {len(dd_rows)} due-diligence rows, {len(kyc_checks)} KYC checks", file=sys.stderr)


def sync_buyers(db: Supabase, profiles: list[dict[str, Any]]) -> None:
    db.upsert("buyers", [profile_row(p, BUYER_COLUMNS) for p in profiles], "id")
    print(f"buyers: {len(profiles)} profiles", file=sys.stderr)


def import_evidence_files(db: Supabase, evidence_dir: Path) -> None:
    """One-off: move data/supplier_evidence/<id>.json documents into supplier_documents."""
    rows = []
    for path in sorted(evidence_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for doc in data.get("documents", []):
            known = {"type", "title", "issuer", "number", "valid_until", "file", "verification_status", "uploaded_at"}
            rows.append({
                "supplier_id": data.get("supplier_id") or path.stem,
                **{k: doc.get(k) for k in known if doc.get(k) not in (None, "")},
                "data": {k: v for k, v in doc.items() if k not in known},
            })
    if rows:
        db.insert("supplier_documents", rows)
    print(f"supplier documents imported: {len(rows)}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Upsert data/profiles/*.json into Supabase.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--due-diligence-only", action="store_true", help="only push due_diligence, kyc and deal_baseline")
    parser.add_argument("--import-evidence", action="store_true", help="also import data/supplier_evidence/*.json documents (one-off)")
    args = parser.parse_args()

    db = Supabase.from_env()
    if db is None:
        parser.error("set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (environment or .env)")
    suppliers = json.loads((args.data_dir / "profiles" / "suppliers.json").read_text(encoding="utf-8"))
    sync_suppliers(db, suppliers, args.due_diligence_only)
    if not args.due_diligence_only:
        buyers_path = args.data_dir / "profiles" / "buyers.json"
        if buyers_path.exists():
            sync_buyers(db, json.loads(buyers_path.read_text(encoding="utf-8")))
    if args.import_evidence:
        import_evidence_files(db, args.data_dir / "supplier_evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
