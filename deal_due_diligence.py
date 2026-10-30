#!/usr/bin/env python3
"""Per-deal due diligence D1-D14 for (open tender, candidate supplier) pairs.

A deal = one open TED tender in our four weapons/ammunition classes + one supplier from
data/profiles/suppliers.json that has delivered the same kind of item. Each check returns
pass · condition · escalate · fail · info with a summary, evidence and contract conditions.

Decision (knowledge_base/22-kyc-and-deal-due-diligence.md):
- any fail     → NO-GO
- any escalate → ESCALATE (a human decides or fixes it)
- any condition → GO WITH CONDITIONS (written into the contract draft)
- otherwise    → GO

Inputs: Supabase ted_open_tenders + ted_notices (TED sync), TED Search API for tender details,
supplier profiles with due_diligence/kyc/deal_baseline, Supabase supplier_documents and
agent_mandates (falls back to the default platform policy below). Deal facts the tender does not
state (offer price, delivery date, Incoterms) are treated as unknown and become conditions.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import dd_sources as src
import deal_rules as rules
from supabase_sync import Supabase

METHOD_VERSION = "deal-v1"
CANDIDATES_PER_TENDER = 3
KYC_MAX_AGE_DAYS = 90
TENDER_FIELDS = [
    "publication-number", "notice-title", "title-proc", "description-proc", "description-lot", "buyer-name",
    "buyer-country", "buyer-legal-type", "main-activity", "classification-cpv", "legal-basis", "procedure-type",
    "estimated-value-lot", "estimated-value-cur-lot", "quantity-lot", "quantity-unit-lot", "guarantee-required-lot",
    "guarantee-required-description-lot", "security-clearance-lot", "eu-funds-programme-lot", "eu-funds-name",
    "duration-period-value-lot", "duration-period-unit-lot", "framework-agreement-lot",
    "place-of-performance-country-lot", "penalties-rewards-description-res", "deadline-receipt-tender-date-lot",
    "deadline-receipt-request",
]
# Default platform policy until each company signs its own agent mandate (K11).
DEFAULT_MANDATE = {
    "source": "default platform policy (no signed mandate yet)",
    "max_autonomous_value_eur": 250_000,
    "min_supplier_score": 6.0,
    "allowed_kyc_status": ["verified", "limited"],
    "allowed_cpv_prefixes": ["3531", "3532", "3533", "3534"],
}


# --------------------------------------------------------------------------- helpers


def check(code: str, title: str, gate: str, status: str, summary: str, evidence: list[str] | None = None,
          conditions: list[str] | None = None) -> dict[str, Any]:
    return {"code": code, "title": title, "gate": gate, "status": status, "summary": summary,
            "evidence": evidence or [], "conditions": conditions or []}


def text_of(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("eng") or next(iter(value.values()), "")
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value or "")


def first(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("eng") or next(iter(value.values()), [])
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def to_eur(value: float | None, currency: str | None, rates: dict[str, float]) -> float | None:
    if value in (None, 0):
        return None
    rate = rates.get(currency or "EUR")
    return float(value) / rate if rate else None


def parse_day(value: str | None) -> date | None:
    try:
        return date.fromisoformat((value or "")[:10])
    except ValueError:
        return None


# --------------------------------------------------------------------------- data loading


def load_tenders(db: Supabase | None, only: str | None) -> list[dict[str, Any]]:
    if db is None:
        raise SystemExit("Supabase credentials missing (.env): open tenders come from ted_open_tenders")
    query = "select=*&order=next_deadline.asc"
    if only:
        query += f"&publication_number=eq.{only}"
    tenders = db.select("ted_open_tenders", query)
    numbers = [t["publication_number"] for t in tenders]
    details: dict[str, dict[str, Any]] = {}
    for start in range(0, len(numbers), 50):
        chunk = " ".join(numbers[start:start + 50])
        body = {"query": f"publication-number IN ({chunk})", "fields": TENDER_FIELDS, "limit": 100}
        data, _ = src.http("https://api.ted.europa.eu/v3/notices/search", data=body)
        for notice in (data or {}).get("notices", []) if isinstance(data, dict) else []:
            details[notice["publication-number"]] = notice
    for tender in tenders:
        tender["ted"] = details.get(tender["publication_number"], {})
    return tenders


def load_history(db: Supabase) -> list[dict[str, Any]]:
    rows, offset = [], 0
    while True:
        page = db.select("ted_notices", f"select=publication_number,kind,publication_date,buyer_name,buyer_country,cpv_codes,"
                                        f"estimated_value,estimated_currency,result_value,result_currency,legal_basis&order=publication_number&limit=1000&offset={offset}")
        rows += page
        if len(page) < 1000:
            return rows
        offset += 1000


def load_documents(db: Supabase | None) -> dict[str, list[dict[str, Any]]]:
    by_supplier: dict[str, list[dict[str, Any]]] = {}
    if db is None:
        return by_supplier
    for row in db.select("supplier_documents", "select=*"):
        by_supplier.setdefault(row["supplier_id"], []).append({**(row.get("data") or {}), **row})
    return by_supplier


def load_mandates(db: Supabase | None) -> dict[str, dict[str, Any]]:
    if db is None:
        return {}
    try:
        return {m["company_id"]: dict(m, source="signed agent mandate") for m in db.select("agent_mandates", "select=*&active=eq.true")}
    except RuntimeError:
        return {}  # table not created yet


def candidates_for(tender: dict[str, Any], suppliers: list[dict[str, Any]], count: int) -> list[tuple[dict[str, Any], str]]:
    """Suppliers that delivered the same CPV (best), class (4 digits) or weapons family."""
    codes = tender.get("cpv_codes") or []
    scored = []
    for supplier in suppliers:
        delivered = {c for contract in supplier.get("contracts") or [] for c in contract.get("cpv_codes") or []}
        delivered |= {c["code"] for c in supplier.get("top_cpv") or []}
        exact = [c for c in codes if c in delivered and c != "35300000"]
        same_class = [c for c in codes if any(d[:4] == c[:4] and c[:4] != "3530" for d in delivered)]
        if exact:
            level = "exact CPV delivered before"
        elif same_class:
            level = "same CPV class delivered before"
        else:
            continue
        dd = supplier.get("due_diligence") or {}
        scored.append(((2 if exact else 1), dd.get("total_score", 0), supplier, level))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    return [(s[2], s[3]) for s in scored[:count]]


# --------------------------------------------------------------------------- the checks


def d1_freshness(supplier, sanctions) -> dict[str, Any]:
    kyc = supplier.get("kyc") or {}
    checked = parse_day(kyc.get("kyc_checked_at"))
    names = {supplier.get("legal_name") or supplier["name"]}
    for owner in (kyc.get("checks", {}).get("K4", {}).get("detail") or []):
        if owner.get("name") and not owner["name"].startswith("(") and owner.get("kind") not in ("person", "ubo"):
            names.add(owner["name"])
    hits = [f"{n}: {e['list']} {e['ref']}" for n in names for e in src.screen(n, sanctions)["exact"]]
    evidence = [f"Sanctions lists re-screened now: {', '.join(sorted(sanctions['loaded']))} ({len(names)} names)"]
    if hits:
        return check("D1", "KYC fresh; sanctions re-screened now", "HARD", "fail", "Sanctions match on re-screen: " + "; ".join(hits), evidence)
    if kyc.get("kyc_status") == "blocked":
        return check("D1", "KYC fresh; sanctions re-screened now", "HARD", "fail", "Supplier KYC is blocked", evidence)
    if not checked or (date.today() - checked).days > KYC_MAX_AGE_DAYS:
        return check("D1", "KYC fresh; sanctions re-screened now", "HARD", "escalate", f"KYC older than {KYC_MAX_AGE_DAYS} days ({checked}) — re-run KYC", evidence)
    open_hard = [i for i in kyc.get("open_items", []) if i.split(":")[0] in ("K1", "K2", "K5")]
    status = "condition" if open_hard else "pass"
    return check("D1", "KYC fresh; sanctions re-screened now", "HARD", status,
                 f"KYC {kyc.get('kyc_status')} from {checked}; no sanctions match now" + (f"; open: {', '.join(open_hard)}" if open_hard else ""),
                 evidence, [f"Close KYC item before signature: {i}" for i in open_hard])


def d2_item_allowed(tender, supplier) -> dict[str, Any]:
    codes = tender.get("cpv_codes") or []
    rule = rules.cpv_rule(codes)
    text = (tender.get("title_en") or "") + " " + text_of(tender["ted"].get("description-lot")) + " " + text_of(tender.get("title_original"))
    heavier = [ml for ml, words in rules.HEAVY_ITEM_WORDS.items() if any(w in text.lower() for w in words)]
    ml = rule["ml"] + (f" (text suggests {', '.join(heavier)})" if heavier else "")
    buyer = tender.get("buyer_name") or ""
    activity = first(tender["ted"].get("main-activity"))
    state_buyer = activity in rules.STATE_SECURITY_ACTIVITIES or any(w in src.fold(buyer) for w in rules.STATE_SECURITY_WORDS)
    countries = {tender.get("buyer_country"), supplier["country"]}
    embargoed = [rules.EU_ARMS_EMBARGO[c] for c in countries if c in rules.EU_ARMS_EMBARGO]
    evidence = [f"CPV {', '.join(codes)} → {ml}; civilian category: {rule['civil']}", f"Buyer: {buyer} (main activity: {activity or '?'})"]
    if embargoed:
        return check("D2", "Item allowed for this buyer; embargoes", "HARD", "fail", f"EU arms embargo applies: {', '.join(embargoed)}", evidence)
    if state_buyer:
        return check("D2", "Item allowed for this buyer; embargoes", "HARD", "pass",
                     f"{ml} item for a state defence/security buyer — may acquire military items; no embargo on {', '.join(sorted(c for c in countries if c))}", evidence)
    if rule["civil"] == "A" or rule["civil"] == "military only":
        return check("D2", "Item allowed for this buyer; embargoes", "HARD", "fail",
                     f"{ml} item (civilian category {rule['civil']}) for a non-state buyer — not allowed without a special authorisation", evidence)
    return check("D2", "Item allowed for this buyer; embargoes", "HARD", "condition",
                 f"Non-state buyer for a {ml} item — buyer must hold an acquisition authorisation (Firearms Directive cat {rule['civil']})",
                 evidence, ["Buyer provides its firearms/ammunition acquisition authorisation"])


def d3_licences(tender, supplier, documents) -> dict[str, Any]:
    origin, destination = supplier["country"], tender.get("buyer_country") or ""
    rule = rules.cpv_rule(tender.get("cpv_codes") or [])
    route = rules.land_route(origin, destination)
    transit = route[1:-1] if route else []
    have = {d["type"] for d in documents}
    needed, evidence = [], [f"Flow {origin} → {destination}" + (f" via {', '.join(transit)}" if transit else "")]
    k7 = ((supplier.get("kyc") or {}).get("checks", {}).get("K7") or {}).get("status")
    if k7 != "pass":
        needed.append("Supplier's home-state licence to trade/manufacture weapons (K7 not yet passed)")
    if origin == destination:
        evidence.append("Domestic sale: national licences only")
    elif origin in rules.EU and destination in rules.EU:
        needed.append(f"Intra-EU transfer licence from {origin} (Directive 2009/43/EC); a general transfer licence may cover armed-forces recipients")
        if "civil" in rule and rule["civil"] not in ("military only",):
            evidence.append("If supplied as civilian firearms/ammunition: prior consent under the Firearms Directive transfer procedure")
    else:
        needed.append(f"Export licence from {origin} (non-EU supplier) and import licence in {destination}")
        if origin == "USA":
            needed.append("US export authorisation (ITAR/EAR) — outside platform scope")
    if transit:
        needed.append(f"Transit authorisations for military goods in {', '.join(transit)}")
    needed.append("End-user certificate signed by the buyer")
    if (supplier.get("deal_baseline") or {}).get("D12_integrity_signals", {}).get("registered_as_trader_not_manufacturer"):
        needed.append("Broker registration if the supplier brokers rather than holds stock (Common Position 2003/468/CFSP)")
    if "export_licence" in have or "arms_trade_licence" in have:
        evidence.append("Supplier uploaded: " + ", ".join(sorted(have & {"export_licence", "arms_trade_licence", "certider"})))
    return check("D3", "Licences for this flow", "HARD", "condition", f"{len(needed)} licence/document condition(s) for this flow", evidence, needed)


def d4_data_rules(tender, supplier) -> dict[str, Any]:
    tender_text = " ".join([
        tender.get("title_en") or "", text_of(tender.get("title_original")), text_of(tender["ted"].get("description-lot")),
        text_of(tender["ted"].get("description-proc")),
    ])
    catalogue_text = json.dumps(supplier.get("catalogue") or {}, ensure_ascii=False)
    export_hits = rules.find_terms(rules.EXPORT_CONTROL_WORDS, tender_text)
    classified_hits = rules.find_terms(rules.CLASSIFIED_WORDS, tender_text)
    catalogue_hits = rules.find_terms(rules.EXPORT_CONTROL_WORDS, catalogue_text)
    clearance = first(tender["ted"].get("security-clearance-lot"))
    evidence = [f"Scanned tender title/description; security clearance required: {clearance or 'not stated'}"]
    if catalogue_hits:
        evidence.append("Supplier catalogue mentions: " + ", ".join(catalogue_hits))
    if classified_hits or clearance == "true":
        return check("D4", "No ITAR/EAR, classified or CUI data", "HARD", "escalate",
                     "Classified information involved (" + ", ".join(classified_hits or ["security clearance required"]) + ") — handle outside the platform", evidence)
    if export_hits:
        return check("D4", "No ITAR/EAR, classified or CUI data", "HARD", "escalate",
                     "Possible US-controlled item (" + ", ".join(export_hits) + ") — confirm ITAR/EAR status off-platform", evidence)
    if catalogue_hits:
        return check("D4", "No ITAR/EAR, classified or CUI data", "HARD", "condition", "Tender is clean; supplier's catalogue mentions US export-control terms",
                     evidence, ["Supplier confirms the offered item is not ITAR/EAR-controlled"])
    return check("D4", "No ITAR/EAR, classified or CUI data", "HARD", "pass", "No export-control or classification markers found", evidence)


def d5_technical_fit(tender, supplier, level, documents, delivery_by) -> dict[str, Any]:
    evidence = [f"Match: {level}"]
    conditions = ["Offer states calibre/standard/variant and the acceptance tests (lot acceptance, AQAP clause if required)"]
    certs = [d for d in documents if d["type"] in ("iso_9001", "en_9100", "aqap")]
    expiring = [d for d in certs if d.get("valid_until") and delivery_by and parse_day(d["valid_until"]) and parse_day(d["valid_until"]) < delivery_by]
    if expiring:
        conditions.append("Renew before delivery: " + ", ".join(f"{d['type']} (valid until {d['valid_until']})" for d in expiring))
    if not certs:
        conditions.append("Upload ISO 9001 / EN 9100 certificate valid through delivery")
    status = "condition"
    summary = f"Supplier has {level}; certificates on file: {len(certs)}"
    return check("D5", "Technical fit; certificates valid at delivery", "HARD/SCORE", status, summary, evidence, conditions)


def d6_capacity(tender, supplier, value_eur) -> dict[str, Any]:
    baseline = supplier.get("deal_baseline") or {}
    largest = (baseline.get("D6_capacity_hints") or {}).get("largest_lot_eur")
    revenue = (baseline.get("D10_financial_capacity") or {}).get("revenue_eur")
    quantity = first(tender["ted"].get("quantity-lot"))
    unit = first(tender["ted"].get("quantity-unit-lot"))
    evidence = [f"Largest past lot on TED: €{largest:,.0f}" if largest else "No valued past lot on TED",
                f"Tender quantity: {quantity} {unit}".strip() if quantity else "Tender quantity not stated in the notice"]
    conditions = ["Dated capacity statement: free monthly output, lead time, delivery schedule vs deadline"]
    if value_eur and largest:
        ratio = value_eur / largest
        evidence.append(f"Deal value €{value_eur:,.0f} = {ratio:.1f}× largest past lot")
        if ratio > 5 and (not revenue or value_eur > revenue * 0.5):
            return check("D6", "Capacity and schedule vs deadline", "SCORE/HARD", "escalate",
                         f"Deal is {ratio:.0f}× the largest lot this supplier has delivered — capacity unproven", evidence, conditions)
        if ratio > 2:
            conditions.append("Split delivery into batches with acceptance per batch")
    return check("D6", "Capacity and schedule vs deadline", "SCORE/HARD", "condition",
                 "Past deliveries known; current free capacity must be declared", evidence, conditions)


def d7_supply_chain(tender, supplier) -> dict[str, Any]:
    funds = (text_of(tender["ted"].get("eu-funds-programme-lot")) + " " + text_of(tender["ted"].get("eu-funds-name"))).lower()
    safe = "safe" in funds or "2025/1106" in funds
    owners = (supplier.get("kyc") or {}).get("checks", {}).get("K4", {}).get("detail") or []
    owner_countries = {o.get("country") for o in owners if o.get("country")}
    origin = supplier["country"]
    evidence = [f"Supplier established in {origin}; owner/parent countries {sorted(owner_countries) or 'unknown'}",
                f"EU funding stated in notice: {funds.strip() or 'none'}"]
    conditions = ["Origin declaration for critical inputs (propellant, primers, explosives, brass/steel) and single-source sub-suppliers"]
    if safe and origin not in rules.SAFE_ELIGIBLE:
        return check("D7", "Supply chain and origin (SAFE ≤35% non-EU)", "SCORE/HARD", "fail",
                     f"SAFE-funded tender but supplier is established in {origin} (not EU/EEA-EFTA/Ukraine)", evidence)
    if safe:
        conditions.append("Bill of materials showing ≤35% component cost from outside EU/EEA-EFTA/Ukraine (Reg. (EU) 2025/1106)")
    non_eu_owner = [c for c in owner_countries if len(c) == 2 and c not in {"PL", "CZ", "SK", "DE", "FR", "ES", "IT", "NL", "BE", "LU", "AT", "DK", "SE", "FI",
                                                                            "EE", "LV", "LT", "PT", "IE", "SI", "HR", "HU", "RO", "BG", "GR", "CY", "MT", "NO", "IS", "LI"}]
    if origin not in rules.SAFE_ELIGIBLE or non_eu_owner:
        conditions.append(f"Security-of-supply plan: non-EU establishment/ownership ({origin}{', owners ' + ', '.join(non_eu_owner) if non_eu_owner else ''})")
    return check("D7", "Supply chain and origin (SAFE ≤35% non-EU)", "SCORE/HARD", "condition",
                 ("SAFE funding detected; " if safe else "No SAFE funding stated; ") + "origin declaration required", evidence, conditions)


def d8_transport(tender, supplier) -> dict[str, Any]:
    rule = rules.cpv_rule(tender.get("cpv_codes") or [])
    origin, destination = supplier["country"], tender.get("buyer_country") or ""
    route = rules.land_route(origin, destination)
    evidence = [f"Dangerous goods: {rule['adr']}"]
    conditions = []
    if route:
        evidence.append("Likely land route: " + " → ".join(route))
        if "GBR" in route and "FRA" in route:
            conditions.append("Channel Tunnel does not take most Class 1 goods — use a ferry crossing")
    else:
        evidence.append(f"No land route {origin} → {destination}: sea or air freight")
        conditions.append("Sea/air carrier with IMDG/IATA dangerous-goods approval")
    if rule["adr"].startswith("Class 1"):
        conditions.append("ADR Class 1 carrier and vehicles, driver training certificate, explosives-storage licence at the delivery site")
    conditions.append("Incoterms agreed (default proposal: DAP buyer's site) and access arrangements for the military/police site")
    return check("D8", "Transport and logistics", "HARD/SCORE", "condition", f"{len(conditions)} transport condition(s)", evidence, conditions)


def d9_price(tender, history, rates, value_eur) -> dict[str, Any]:
    codes = [c for c in tender.get("cpv_codes") or [] if c != "35300000"] or tender.get("cpv_codes") or []
    prefixes = {c[:5] for c in codes}
    values = []
    for notice in history:
        if notice.get("kind") not in ("award", "result", "can") and notice.get("result_value") is None:
            continue
        if not any(c[:5] in prefixes for c in notice.get("cpv_codes") or []):
            continue
        eur = to_eur(notice.get("result_value"), notice.get("result_currency"), rates)
        if eur and eur > 1000:
            values.append(eur)
    evidence = [f"{len(values)} awarded TED notices in CPV {', '.join(sorted(p + '…' for p in prefixes))}"]
    if not values:
        return check("D9", "Price sanity vs history", "SCORE", "condition", "No comparable award values in TED history", evidence,
                     ["Price justified by the supplier with unit-price breakdown"])
    values.sort()
    median = statistics.median(values)
    evidence.append(f"Award values: median €{median:,.0f}, range €{values[0]:,.0f}–€{values[-1]:,.0f}")
    if value_eur:
        position = sum(1 for v in values if v <= value_eur) / len(values)
        evidence.append(f"Tender estimate €{value_eur:,.0f} is at the {position:.0%} percentile")
    return check("D9", "Price sanity vs history", "SCORE", "info",
                 "Baseline ready; compare the offer's unit price to the buyer's estimate and history (lot values, not unit prices)", evidence)


def d10_financial(tender, supplier, value_eur) -> dict[str, Any]:
    finance = (supplier.get("deal_baseline") or {}).get("D10_financial_capacity") or {}
    revenue = finance.get("revenue_eur")
    guarantee = first(tender["ted"].get("guarantee-required-lot")) == "true"
    evidence = [f"Revenue: €{revenue:,.0f} ({finance.get('revenue_year')})" if revenue else "Revenue not public",
                f"Tender requires a guarantee: {'yes — ' + first(tender['ted'].get('guarantee-required-description-lot'))[:120] if guarantee else 'not stated'}"]
    conditions = ["Bid/performance guarantee as required by the tender"] if guarantee else []
    if value_eur and revenue:
        share = value_eur / revenue
        evidence.append(f"Deal value = {share:.0%} of annual revenue")
        if share > 1:
            return check("D10", "Payment terms and financial capacity", "SCORE", "escalate",
                         f"Deal is {share:.1f}× annual revenue — financing risk", evidence, conditions + ["Parent/bank guarantee and milestone payments"])
        if share > 0.3:
            conditions.append("Advance payment or milestone payments; performance guarantee")
    elif value_eur:
        conditions.append("Financial statements or bank reference (revenue not public)")
    status = "condition" if conditions else "pass"
    return check("D10", "Payment terms and financial capacity", "SCORE", status,
                 "Financial capacity " + ("checked against revenue" if revenue else "not provable from open data"), evidence, conditions)


def d11_contract_terms(tender) -> dict[str, Any]:
    ted = tender["ted"]
    framework = first(ted.get("framework-agreement-lot"))
    duration = " ".join(filter(None, [first(ted.get("duration-period-value-lot")), first(ted.get("duration-period-unit-lot")).lower()]))
    penalties = first(ted.get("penalties-rewards-description-res"))
    evidence = [f"Framework agreement: {framework or 'not stated'}", f"Duration: {duration or 'not stated'}", f"Legal basis: {tender.get('legal_basis')}"]
    conditions = [
        "Warranty period and acceptance testing stated in the offer",
        "Rights to technical documentation and spare parts for the service life (avoid sole-source lock-in)",
        "Penalties for late delivery mirrored to the supplier's sub-suppliers",
    ]
    if framework and framework != "none":
        conditions.append("Framework: no guaranteed volume — price validity and call-off lead time agreed")
    if penalties:
        evidence.append("Penalties in notice: " + penalties[:150])
    return check("D11", "Contract terms", "SCORE", "condition", "Standard conditions for the contract draft", evidence, conditions)


def d12_integrity(tender, supplier, other_candidates, history, rates) -> dict[str, Any]:
    signals = (supplier.get("deal_baseline") or {}).get("D12_integrity_signals") or {}
    related = set(signals.get("related_suppliers_same_owner") or []) & {c["id"] for c in other_candidates}
    evidence = [f"Single-bid share of past wins: {signals.get('single_bid_share')}",
                f"Registered as trader, not manufacturer: {signals.get('registered_as_trader_not_manufacturer')}"]
    conditions = []
    if related:
        return check("D12", "Integrity: related parties, brokers, split orders", "HARD/SCORE", "escalate",
                     "Another candidate for this tender shares an owner: " + ", ".join(sorted(related)), evidence)
    if signals.get("registered_as_trader_not_manufacturer"):
        conditions.append("Name the manufacturer and provide its authorisation/distribution agreement")
    if (signals.get("single_bid_share") or 0) >= 0.8:
        conditions.append("Most past wins were single-bid — buyer should confirm competitive pricing")
    # Split orders: the same buyer buying the same CPV class repeatedly just under the EU threshold.
    threshold = rules.THRESHOLD_SUPPLIES_EUR.get(tender.get("legal_basis") or "", 143_000)
    prefixes = {c[:5] for c in tender.get("cpv_codes") or []}
    small = [n for n in history if n.get("buyer_name") == tender.get("buyer_name")
             and any(c[:5] in prefixes for c in n.get("cpv_codes") or [])
             and (to_eur(n.get("estimated_value"), n.get("estimated_currency"), rates) or 1e12) < threshold]
    if len(small) >= 3:
        evidence.append(f"Buyer published {len(small)} notices in this CPV class under the ~€{threshold:,} threshold")
        conditions.append("Check the order is not split below the EU threshold")
    return check("D12", "Integrity: related parties, brokers, split orders", "HARD/SCORE", "condition" if conditions else "pass",
                 "No related party among candidates" + ("; see conditions" if conditions else ""), evidence, conditions)


def d13_references(tender, supplier) -> dict[str, Any]:
    baseline = supplier.get("deal_baseline") or {}
    buyers = baseline.get("D13_reference_candidates") or []
    same_buyer = [b for b in buyers if src.name_similarity(b["buyer"], tender.get("buyer_name") or "") >= 0.6]
    mods = baseline.get("D13_contract_modifications_on_ted", 0)
    names = ", ".join("{} ({} lots)".format(b["buyer"], b["lots"]) for b in buyers[:3])
    evidence = [f"Reference buyers: {names or 'none'}",
                f"Contract modifications on TED: {mods}"]
    if same_buyer:
        return check("D13", "References from past counterparties", "SCORE", "pass",
                     f"Supplier has delivered to this buyer before ({same_buyer[0]['lots']} lots)", evidence)
    return check("D13", "References from past counterparties", "SCORE", "condition" if buyers else "info",
                 "Ask past buyers for a delivery reference" if buyers else "No past public buyers to ask", evidence,
                 [f"Reference from {buyers[0]['buyer']}"] if buyers else [])


def d14_mandate(tender, supplier, value_eur, mandates) -> dict[str, Any]:
    results = []
    for side, company_id in (("buyer", f"buyer:{tender.get('buyer_name')}"), ("supplier", supplier["id"])):
        mandate = mandates.get(company_id) or DEFAULT_MANDATE
        problems = []
        score = (supplier.get("due_diligence") or {}).get("total_score", 0)
        if side == "buyer" and score < mandate["min_supplier_score"]:
            problems.append(f"supplier score {score} < {mandate['min_supplier_score']}")
        if side == "buyer" and (supplier.get("kyc") or {}).get("kyc_status") not in mandate["allowed_kyc_status"]:
            problems.append("supplier KYC status not allowed")
        if not any(c.startswith(tuple(mandate["allowed_cpv_prefixes"])) for c in tender.get("cpv_codes") or []):
            problems.append("category outside mandate")
        if value_eur is not None and value_eur > mandate["max_autonomous_value_eur"]:
            problems.append(f"value €{value_eur:,.0f} > autonomous limit €{mandate['max_autonomous_value_eur']:,}")
        results.append((side, mandate["source"], problems))
    evidence = [f"{side}: {source}" + (f" — {'; '.join(p)}" if p else " — within limits") for side, source, p in results]
    if any(p for _, _, p in results):
        reasons = sorted({p for _, _, problems in results for p in problems})
        return check("D14", "Deal inside both agents' mandates", "HARD", "escalate", "Outside an agent mandate (" + "; ".join(reasons) + ") — escalate to a human", evidence)
    conditions = []
    if value_eur is None:
        conditions.append(f"Tender has no estimated value: the agent may accept only an offer ≤ €{min(m['max_autonomous_value_eur'] for m in [mandates.get(supplier['id']) or DEFAULT_MANDATE]):,}; above that a human decides")
    if any("default" in source for _, source, _ in results):
        conditions.append("Both parties sign their agent mandate (K11)")
    if conditions:
        return check("D14", "Deal inside both agents' mandates", "HARD", "condition",
                     "Within the default platform policy" + ("; value to be checked at acceptance" if value_eur is None else ""), evidence, conditions)
    return check("D14", "Deal inside both agents' mandates", "HARD", "pass", "Within both signed mandates", evidence)


# --------------------------------------------------------------------------- deal assembly


def run_deal(tender, supplier, level, others, ctx) -> dict[str, Any]:
    rates = ctx["rates"]
    value_eur = to_eur(tender.get("estimated_value"), tender.get("estimated_currency"), rates)
    deadline = parse_day(tender.get("next_deadline"))
    months = first(tender["ted"].get("duration-period-value-lot"))
    unit = first(tender["ted"].get("duration-period-unit-lot")).upper()
    delivery_by = None
    if deadline:
        span = int(months) * (30 if unit == "MONTH" else 365 if unit == "YEAR" else 1) if months.isdigit() else 180
        delivery_by = deadline + timedelta(days=span)
    documents = ctx["documents"].get(supplier["id"], [])
    checks = [
        d1_freshness(supplier, ctx["sanctions"]),
        d2_item_allowed(tender, supplier),
        d3_licences(tender, supplier, documents),
        d4_data_rules(tender, supplier),
        d5_technical_fit(tender, supplier, level, documents, delivery_by),
        d6_capacity(tender, supplier, value_eur),
        d7_supply_chain(tender, supplier),
        d8_transport(tender, supplier),
        d9_price(tender, ctx["history"], rates, value_eur),
        d10_financial(tender, supplier, value_eur),
        d11_contract_terms(tender),
        d12_integrity(tender, supplier, others, ctx["history"], rates),
        d13_references(tender, supplier),
        d14_mandate(tender, supplier, value_eur, ctx["mandates"]),
    ]
    statuses = {c["status"] for c in checks}
    decision = ("NO-GO" if "fail" in statuses else "ESCALATE" if "escalate" in statuses
                else "GO WITH CONDITIONS" if "condition" in statuses else "GO")
    return {
        "deal_id": f"{tender['publication_number']}:{supplier['id']}",
        "method_version": METHOD_VERSION,
        "checked_at": src.today(),
        "tender": {
            "publication_number": tender["publication_number"], "title": tender.get("title_en"), "buyer": tender.get("buyer_name"),
            "buyer_country": tender.get("buyer_country"), "cpv_codes": tender.get("cpv_codes"), "estimated_value_eur": round(value_eur) if value_eur else None,
            "next_deadline": tender.get("next_deadline"), "delivery_by_estimate": delivery_by.isoformat() if delivery_by else None,
            "ted_url": tender.get("ted_url"),
        },
        "supplier": {"id": supplier["id"], "name": supplier["name"], "country": supplier["country"],
                     "score": (supplier.get("due_diligence") or {}).get("total_score"), "kyc_status": (supplier.get("kyc") or {}).get("kyc_status"),
                     "match": level},
        "decision": decision,
        "blocking": [f"{c['code']}: {c['summary']}" for c in checks if c["status"] in ("fail", "escalate")],
        "conditions": [f"{c['code']}: {cond}" for c in checks for cond in c["conditions"]],
        "checks": checks,
    }


def summary_md(deals: list[dict[str, Any]]) -> str:
    icon = {"pass": "✅", "condition": "📝", "escalate": "👤", "fail": "❌", "info": "ℹ️"}
    codes = [f"D{i}" for i in range(1, 15)]
    lines = [
        f"# Deal due diligence — {src.today()} ({METHOD_VERSION})", "",
        "Each row = one open TED tender × one supplier that delivered the same kind of item. "
        "✅ pass · 📝 condition (goes into the contract) · 👤 escalate to a human · ❌ fail (NO-GO) · ℹ️ info", "",
        "| Tender | Buyer | Supplier | Decision | " + " | ".join(codes) + " |",
        "|" + "---|" * (4 + len(codes)),
    ]
    for deal in deals:
        cells = [icon.get(c["status"], c["status"]) for c in deal["checks"]]
        lines.append(f"| [{deal['tender']['publication_number']}]({deal['tender']['ted_url']}) {(deal['tender']['title'] or '')[:50]} | "
                     f"{(deal['tender']['buyer'] or '')[:30]} | {deal['supplier']['name'][:30]} | **{deal['decision']}** | " + " | ".join(cells) + " |")
    lines += ["", "## Why escalated or blocked", ""]
    for deal in deals:
        if deal["blocking"]:
            lines.append(f"- **{deal['tender']['publication_number']} × {deal['supplier']['name']}**: " + " · ".join(deal["blocking"]))
    lines += ["", "> Rule tables (Military List, ADR, embargoes, licences) encode common EU practice and are not legal advice; "
              "verify before relying on them. Quantities, prices and delivery dates not stated in a tender are treated as unknown.", ""]
    return "\n".join(lines)


def push(db: Supabase, deals: list[dict[str, Any]]) -> None:
    rows = [{
        "deal_id": d["deal_id"], "publication_number": d["tender"]["publication_number"], "supplier_id": d["supplier"]["id"],
        "method_version": d["method_version"], "checked_at": d["checked_at"], "decision": d["decision"],
        "tender": d["tender"], "supplier": d["supplier"], "blocking": d["blocking"], "conditions": d["conditions"],
    } for d in deals]
    db.upsert("deal_checks", rows, "deal_id")
    items = [{"deal_id": d["deal_id"], **{k: c[k] for k in ("code", "title", "gate", "status", "summary", "evidence", "conditions")}}
             for d in deals for c in d["checks"]]
    db.upsert("deal_check_items", items, "deal_id,code")
    print(f"pushed {len(rows)} deals, {len(items)} check rows", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deal due diligence D1-D14 on open tenders × proven suppliers.")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--tender", help="only this TED publication number")
    parser.add_argument("--candidates", type=int, default=CANDIDATES_PER_TENDER)
    parser.add_argument("--push", action="store_true", help="upsert results into Supabase deal_checks / deal_check_items")
    args = parser.parse_args()

    db = Supabase.from_env()
    suppliers = json.loads((args.data_dir / "profiles" / "suppliers.json").read_text(encoding="utf-8"))
    print("Loading tenders, TED history, sanctions lists...", file=sys.stderr)
    ctx = {
        "rates": src.ecb_rates(),
        "sanctions": src.sanctions_index(),
        "history": load_history(db),
        "documents": load_documents(db),
        "mandates": load_mandates(db),
    }
    tenders = load_tenders(db, args.tender)
    deals = []
    for tender in tenders:
        candidates = candidates_for(tender, suppliers, args.candidates)
        others = [s for s, _ in candidates]
        for supplier, level in candidates:
            deals.append(run_deal(tender, supplier, level, [o for o in others if o is not supplier], ctx))
    out_dir = args.data_dir / "deals"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "deal_checks.json").write_text(json.dumps(deals, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (out_dir / "deal_checks_summary.md").write_text(summary_md(deals), encoding="utf-8")
    counts = {}
    for deal in deals:
        counts[deal["decision"]] = counts.get(deal["decision"], 0) + 1
    print(f"{len(tenders)} tenders, {len(deals)} deals: {counts}", file=sys.stderr)
    if args.push:
        push(db, deals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
