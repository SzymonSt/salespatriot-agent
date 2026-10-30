"""Build the §3.4 context pack from local profile data (the simulated `agent_context`).

Same shape as the Supabase RPC will return: facts, candidates, evidence, price_anchors,
regulatory, memory, warnings, refs, versions. Unknown stays unknown: never zero, never a guess.
"""

from __future__ import annotations

import re
from typing import Any

from app.backend.due_diligence import initial_due_diligence

CONTEXT_VERSION = "sim-v1"
EVIDENCE_RANK = {"document_verified": 0, "declared": 1, "inferred": 2}
# Product family by CPV prefix, and regulatory defaults (not a licence decision).
FAMILIES = {
    "3533": ("ammunition", {"eu_ml": "ML3", "adr": "Class 1 (small-arms cartridges are often UN0012, 1.4S)"}),
    "3532": ("firearms", {"eu_ml": "ML1 (or ML2 above 12.7 mm)", "adr": None}),
    "3534": ("parts of firearms and ammunition", {"eu_ml": "ML1/ML3 components", "adr": None}),
    "3531": ("miscellaneous weapons", {"eu_ml": "depends on the item", "adr": None}),
}
SINGLE_CALIBRES = {"5.56": "5.56x45", "7.62": "7.62x51", "9": "9x19", "12.7": "12.7x99", "5.45": "5.45x39", "4.6": "4.6x30"}
STOPWORDS = {"with", "from", "and", "for", "the", "of", "mm", "supply", "delivery", "framework", "agreement", "lot"}


def calibres(text: str) -> set[str]:
    """Normalised calibre keys, e.g. {"5.56x45", "9x19"} (mirrors web/agents.js)."""
    text = re.sub(r"(\d),(\d)", r"\1.\2", text or "")
    keys = {f"{a}x{b}" for a, b in re.findall(r"(\d{1,3}(?:\.\d{1,2})?)\s*(?:mm)?\s*[x×]\s*(\d{2,3})", text, re.I)}
    keys |= {SINGLE_CALIBRES[m] for m in re.findall(r"(?:^|[^\d.])(\d{1,2}(?:\.\d{1,2})?)\s*mm", text, re.I) if m in SINGLE_CALIBRES}
    if re.search(r"\.308|308\s*W", text, re.I):
        keys.add("7.62x51")
    return keys


def words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zà-ž]{4,}", (text or "").lower()) if w not in STOPWORDS}


def family(cpv_codes: list[str]) -> tuple[str | None, dict[str, Any]]:
    for code in cpv_codes or []:
        for prefix, (name, regulatory) in FAMILIES.items():
            if code.startswith(prefix):
                return name, regulatory
    return None, {}


def item_text(item: dict[str, Any]) -> str:
    return " ".join([item.get("product", ""), item.get("brand_model", ""), *map(str, (item.get("specs") or {}).values())])


def relevance(tender: dict[str, Any], item: dict[str, Any]) -> float:
    text = f"{tender.get('title', '')} {tender.get('description', '')}"
    score = 3.0 * len(calibres(text) & calibres(item_text(item)))
    if any(code[:4] == (item.get("cpv") or "")[:4] for code in tender.get("cpv_codes") or []):
        score += 2
    score += 0.5 * len(words(text) & words(item_text(item)))
    return score


def parse_price(text: str) -> dict[str, Any] | None:
    match = re.search(r"(EUR|RON|DKK|BGN|USD|SEK|PLN|CZK|GBP|NOK)\s*([\d,]+(?:\.\d+)?)", text or "")
    return {"currency": match.group(1), "value": float(match.group(2).replace(",", ""))} if match else None


def tender_facts(tender: dict[str, Any], role: str = "buyer") -> dict[str, Any]:
    """Tender facts for one side. The buyer's budget is its private mandate: suppliers never see it."""
    family_name, _ = family(tender.get("cpv_codes") or [])
    facts = {
        "ref": tender.get("ref"),
        "title": tender.get("title"),
        "description": (tender.get("description") or "")[:600],
        "buyer": tender.get("buyer_name"),
        "buyer_id": tender.get("buyer_id"),
        "country": tender.get("buyer_country"),
        "cpv_codes": tender.get("cpv_codes") or [],
        "family": family_name,
        "calibres": sorted(calibres(f"{tender.get('title', '')} {tender.get('description', '')}")),
        "quantity": tender.get("quantity"),
        "unit": tender.get("unit"),
        "budget": tender.get("budget") if role == "buyer" else None,
        "currency": tender.get("currency"),
        "need_by": tender.get("need_by"),
        "deal_check": deal_check_summary(tender.get("deal_check")),
    }
    if role != "buyer":
        facts.pop("budget")
        # The deal check quotes the budget (e.g. "€180,000" in D6/D9/D10/D14); mask it too.
        facts["deal_check"] = mask_amount(facts["deal_check"], tender.get("budget"))
    return facts


def mask_amount(value: Any, amount: Any) -> Any:
    """Replace an amount wherever it appears in nested strings (formatted with or without separators)."""
    try:
        number = float(amount)
    except (TypeError, ValueError):
        return value
    shown = sorted({f"{number:,.0f}", f"{number:,.2f}", f"{number:.0f}", f"{number:.2f}"}, key=len, reverse=True)
    if isinstance(value, str):
        for text in shown:
            value = value.replace(text, "[buyer budget]")
        return value
    if isinstance(value, list):
        return [mask_amount(item, amount) for item in value]
    if isinstance(value, dict):
        return {key: mask_amount(item, amount) for key, item in value.items()}
    return value


def deal_check_summary(check: dict[str, Any] | None) -> dict[str, Any] | None:
    """The D1-D14 decision with what blocks it and the first contract conditions."""
    if not check or "decision" not in check:
        return None
    return {
        "decision": check["decision"],
        "blocking": (check.get("blocking") or [])[:5],
        "conditions": (check.get("conditions") or [])[:8],
        "not_ok": [f"{c['code']} {c['status']}: {c['summary']}" for c in check.get("checks") or []
                   if c.get("status") in ("escalate", "fail")],
    }


def supplier_self(supplier: dict[str, Any]) -> dict[str, Any]:
    dd = initial_due_diligence(supplier)
    counts = (supplier.get("catalogue") or {}).get("item_counts") or {}
    track = supplier.get("track_record") or {}
    return {
        "supplier_id": supplier["id"],
        "name": supplier.get("name"),
        "country": supplier.get("country"),
        "proven_lots": track.get("lots_won"),
        "buyer_countries": sorted((track.get("buyer_countries") or {}).keys()),
        "verified_items": counts.get("document_verified", 0),
        "declared_items": counts.get("declared", 0),
        "kyc_score": dd.score,
        "dd_verdict": dd.verdict,
        "dd_decision": dd.decision,
        "kyc_open_items": dd.open_items,
        "risk_flags": [flag.get("flag") for flag in (supplier.get("capabilities") or {}).get("risk_flags") or []],
    }


def build_context(
    profiles: dict[str, dict[str, Any]],
    role: str,
    purpose: str,
    subject: dict[str, Any],
    limit: int = 8,
) -> dict[str, Any]:
    tender = subject.get("tender") or {}
    supplier = profiles.get(subject.get("supplier_id", ""))
    family_name, regulatory = family(tender.get("cpv_codes") or [])
    warnings: list[str] = []
    evidence: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []
    facts: dict[str, Any] = {"tender": tender_facts(tender, role)} if tender else {}

    if supplier:
        facts["self" if role == "supplier" else "counterparty"] = supplier_self(supplier)
        items = (supplier.get("catalogue") or {}).get("items") or []
        ranked = sorted(
            ((relevance(tender, item), index, item) for index, item in enumerate(items)),
            key=lambda entry: (-entry[0], EVIDENCE_RANK.get(entry[2]["evidence"]["verification_status"], 3)),
        )
        for score, index, item in ranked[:limit]:
            ref = f"catalogue:{supplier['id']}/{index}"
            sources = [s.get("notice_number") for s in item["evidence"].get("sources") or [] if s.get("notice_number")]
            evidence.append({
                "ref": ref,
                "kind": "catalogue_item",
                "layer": "platform",
                "title": item.get("product"),
                "snippet": (item["evidence"].get("basis") or "")[:300],
                "evidence_level": item["evidence"]["verification_status"],
                "largest_order": (item.get("track_record") or {}).get("largest_order") or None,
                "ted_refs": [f"ted:{n}" for n in sources[:3]],
                "score": round(score, 2),
            })
            price = parse_price((item.get("commercial") or {}).get("derived_unit_price") or "")
            if price:
                anchors.append({
                    "ref": ref, "layer": "platform", "title": item.get("product"),
                    "unit_price": price["value"], "currency": price["currency"],
                    "basis": "awarded TED unit price (derived)",
                })
        if not any(e["score"] > 0 for e in evidence):
            warnings.append("No catalogue item shares a calibre, product family or key words with the tender")
        if not anchors:
            warnings.append("No awarded unit price for a matching item: price unknown")

    if tender and not tender.get("quantity"):
        warnings.append("Quantity not stated")
    if family_name is None and tender:
        warnings.append("Product family unknown (no weapons/ammunition CPV)")
    refs = [r for r in [tender.get("ref")] + [e["ref"] for e in evidence] + [a["ref"] for a in anchors] if r]
    return {
        "purpose": purpose,
        "subject": {k: v for k, v in subject.items() if k != "tender"} | ({"tender_ref": tender.get("ref")} if tender else {}),
        "facts": facts,
        "candidates": [],
        "evidence": evidence,
        "price_anchors": anchors,
        "regulatory": {**regulatory, "verified": False, "note": "defaults, not a licence decision"} if regulatory else {},
        "memory": [],
        "warnings": warnings,
        "refs": list(dict.fromkeys(refs)),
        "versions": {"context": CONTEXT_VERSION, "kb": "local-profiles"},
    }
