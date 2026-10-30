#!/usr/bin/env python3
"""Score each supplier 1-10 from free open data, with a sub-score and reasoning per category.

Method (dd-v1):
- Six categories, each scored 0-10 with short reasoning and source evidence:
  identity 15%, ownership 20%, track record 25%, financial 15%, defence credentials 15%,
  integrity 10%. A category without usable data is "no data" and is left out of the
  weighted mean (its weight is redistributed) instead of being scored as bad.
- Total = 1 + 9 * weighted mean / 10, so 1.0 is the floor and 10.0 the ceiling.
- Hard gates override the total with 1.0: sanctions name match (pending manual review),
  Russian/Belarusian ownership (Reg. (EU) 833/2014 Art. 5k), insolvency/liquidation,
  dissolved or suspended company.
- Confidence = share of weight backed by data (plus a national-register bonus). Low confidence
  caps the total at 6.0, medium at 8.5, so a thin file can never look excellent.
- Supplier-provided documents (data/supplier_evidence/<supplier id>.json) add
  points as "declared" (60%) or "document_verified" (100%, after a reviewer checks them).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import dd_sources as src

METHOD_VERSION = "dd-v1"
WEIGHTS = {
    "identity": 0.15,
    "ownership": 0.20,
    "track_record": 0.25,
    "financial": 0.15,
    "defence_credentials": 0.15,
    "integrity": 0.10,
}
LABELS = {
    "identity": "Identity & legal standing",
    "ownership": "Ownership & control",
    "track_record": "Track record (TED awards)",
    "financial": "Financial health",
    "defence_credentials": "Defence credentials",
    "integrity": "Integrity & reputation",
}
HIGH_RISK_OWNER_COUNTRIES = {"RU", "BY"}
NON_ALLIED_OWNER_COUNTRIES = {"CN", "IR", "KP", "SY", "VE", "CU", "MM", "AF"}
DEFENCE_NACE = ("2540", "2051", "3040", "3030", "2630")
DEFENCE_WORDS = re.compile(
    r"zbra|stre[l]iv|vojensk|broni|amunic|wojsk|armes?\b|munition|waffe|weapon|ammun|militar|defen[cs]e|obran|explosiv|výbuš|vybus",
    re.I,
)
DEFENCE_BUYER_WORDS = re.compile(
    r"defen[cs]|obrany|obrony|verteidigung|bundeswehr|baainbw|militar|wojsk|uzbrojen|armed forces|forsvar|"
    r"försvar|puolustus|unitatea militar|ministerul apararii|kariuomen|aizsardz|kaitse|police|policie|polici|"
    r"policja|polizei|guardia civil|ejercito|ejército|armée|esercito|mando de apoyo|vnitra|ministerstvo vnitra|"
    r"ministry of interior|innenministerium|gendarm|carabinier",
    re.I,
)
# Document types a supplier can upload, with the points they add when valid.
DOCUMENT_POINTS = {
    "arms_trade_licence": ("defence_credentials", 2.5, "Licence to manufacture/trade military material or firearms"),
    "export_licence": ("defence_credentials", 2.0, "Export licence for military goods"),
    "facility_security_clearance": ("defence_credentials", 2.0, "Facility security clearance (FSC)"),
    "aqap": ("defence_credentials", 1.5, "AQAP 2110/2310 compliance"),
    "ncage": ("defence_credentials", 1.5, "NATO NCAGE code"),
    "certider": ("defence_credentials", 1.5, "Certified defence recipient under Directive 2009/43 (CERTIDER)"),
    "nspa_source_file": ("defence_credentials", 1.0, "NSPA Source File registration"),
    "iso_9001": ("defence_credentials", 1.0, "ISO 9001 quality management"),
    "en_9100": ("defence_credentials", 1.0, "EN/AS 9100 aerospace quality management"),
    "customer_reference": ("track_record", 0.5, "Customer reference letter"),
    "financial_statements": ("financial", 0.0, "Annual financial statements"),
    "ubo_declaration": ("ownership", 0.0, "Declaration of shareholders / beneficial owners"),
}
DECLARED_FACTOR = 0.6
ISO3_TO_ISO2_EXTRA = {"RUS": "RU", "BLR": "BY", "CHN": "CN", "IRN": "IR", "PRK": "KP", "SYR": "SY", "TUR": "TR", "ISR": "IL", "CAN": "CA"}
KYC_REFRESH_DAYS = 90
LICENCE_WORDS = re.compile(r"povolen|licenc|licenz|koncese|koncesj|koncesi|zezwol|permit|genehmigung|autoris|autoriz", re.I)
SIZE_REVENUE_CEILING_EUR = {"micro": 2e6, "small": 10e6, "sme": 50e6, "medium": 50e6}
DISCLAIMER = (
    "Automated screening of free public data on the date shown. It is not legal advice, not a "
    "security clearance and not proof of innocence: 'no record found' only means the named sources "
    "returned nothing. Possible matches must be reviewed by a person before any decision, and the "
    "supplier may respond and correct the data."
)


# --------------------------------------------------------------------------- helpers


class Category:
    def __init__(self, key: str) -> None:
        self.key = key
        self.points = 0.0
        self.has_data = False
        self.findings: list[str] = []
        self.evidence: list[dict[str, str]] = []
        self.forced: float | None = None
        self.red_flags: list[str] = []

    def add(self, points: float, finding: str) -> None:
        self.points += points
        self.findings.append(finding)
        self.has_data = True

    def flag(self, points: float, finding: str) -> None:
        """A negative finding: costs points and is listed as a red flag for the buyer."""
        self.add(points, finding)
        self.red_flags.append(finding)

    def note(self, finding: str) -> None:
        self.findings.append(finding)

    def score(self) -> float | None:
        if self.forced is not None:
            return self.forced
        if not self.has_data:
            return None
        return round(max(0.0, min(10.0, self.points)), 1)

    def output(self) -> dict[str, Any]:
        score = self.score()
        return {
            "label": LABELS[self.key],
            "weight": WEIGHTS[self.key],
            "score": score,
            "reasoning": reasoning(self.findings, self.red_flags, score),
            "findings": self.findings,
            "red_flags": self.red_flags,
            "evidence": self.evidence,
        }


def reasoning(findings: list[str], red_flags: list[str], score: float | None) -> str:
    """One sentence: the two strongest positives, then the first red flag."""
    if score is None:
        return "No usable open data for this category; excluded from the total and lowers confidence."
    positives = [f for f in findings if not f.startswith("(") and f not in red_flags][:2]
    lead = positives + red_flags[:1]
    return "; ".join(lead) + "." if lead else ""


def to_eur(amounts: dict[str, float], rates: dict[str, float]) -> float | None:
    total = 0.0
    for currency, value in (amounts or {}).items():
        rate = rates.get(currency or "EUR")
        if not rate:
            return None
        total += value / rate
    return total


def years_between(start: str, end: str | None = None) -> float | None:
    try:
        first = date.fromisoformat(start[:10])
    except (TypeError, ValueError):
        return None
    last = date.fromisoformat(end[:10]) if end else date.today()
    return (last - first).days / 365.25


def load_documents(evidence_dir: Path, supplier_id: str) -> list[dict[str, Any]]:
    """Supplier uploads: Supabase table supplier_documents when configured, else JSON files."""
    from supabase_sync import Supabase

    db = Supabase.from_env()
    if db is not None:
        rows = db.select("supplier_documents", f"supplier_id=eq.{supplier_id}&select=*")
        return [{**(row.get("data") or {}), **{k: v for k, v in row.items() if k != "data" and v is not None}} for row in rows]
    path = evidence_dir / f"{supplier_id}.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("documents", [])


def document_status(doc: dict[str, Any]) -> tuple[float, str]:
    """Return (factor, label); factor 0 for expired documents."""
    valid_until = doc.get("valid_until") or ""
    if valid_until and valid_until < date.today().isoformat():
        return 0.0, f"expired {valid_until}"
    if doc.get("verification_status") == "document_verified":
        return 1.0, "document verified by reviewer"
    return DECLARED_FACTOR, "declared by supplier, not yet verified"


# --------------------------------------------------------------------------- collection


def collect(profile: dict[str, Any], sanctions: dict[str, Any], with_media: bool) -> dict[str, Any]:
    # Match registers and sanctions lists on the registered name, not an English display name.
    country, identifier, name = profile["country"], profile.get("identifier", ""), legal_name(profile)
    data: dict[str, Any] = {"registry": None, "sources": []}
    fetcher = src.NATIONAL_REGISTERS.get(country)
    if fetcher and identifier:
        data["registry"] = fetcher(identifier)
        data["sources"].append(fetcher.__name__)
    data["vies"] = src.vies(country, identifier) if identifier else None
    data["gleif"] = src.gleif(name, country, identifier)
    gleif = data["gleif"] if isinstance(data["gleif"], dict) and "lei" in data["gleif"] else None
    lei = gleif["lei"] if gleif else None
    # TED identifiers are sometimes wrong; retry VIES with the register id that GLEIF holds.
    registered_as = re.sub(r"[^0-9A-Za-z]", "", (gleif or {}).get("registered_as") or "")
    vies_ok = isinstance(data["vies"], dict) and data["vies"].get("valid")
    if registered_as and not vies_ok and registered_as.upper() != re.sub(r"[^0-9A-Za-z]", "", identifier).upper():
        retry = src.vies(country, registered_as)
        if isinstance(retry, dict) and retry.get("valid"):
            data["ted_identifier_invalid"] = identifier
            data["vies"] = retry
    data["wikidata"] = src.wikidata(lei, name, country)
    ultimate = ((gleif or {}).get("parents") or {}).get("ultimate-parent") or ((gleif or {}).get("parents") or {}).get("direct-parent")
    data["parent_wikidata"] = src.wikidata(ultimate["lei"], ultimate["name"], country) if ultimate else None
    data["eu_funding"] = src.eu_funding(name)
    data["ted_owner_nationality"] = src.ted_owner_nationality(identifier, name)
    data["media"] = src.adverse_media(name) if with_media else None
    data["sources"] += ["vies", "gleif", "wikidata", "eu_funding"] + (["gdelt"] if with_media else [])

    names_to_screen = {name}
    registry = data["registry"] if isinstance(data["registry"], dict) and "error" not in data["registry"] else None
    if registry and registry.get("name"):
        names_to_screen.add(registry["name"])
    owners, officers = owner_list(data), list((registry or {}).get("officers") or [])
    for owner in owners:
        if owner.get("name") and not owner["name"].startswith("("):
            names_to_screen.add(owner["name"])
    data["screening"] = {
        "company_and_owner_names": {n: src.screen(n, sanctions) for n in names_to_screen},
        "persons": {n: src.screen(n, sanctions) for n in officers if n},
        "lists": sanctions["loaded"],
    }
    return data


def legal_name(profile: dict[str, Any]) -> str:
    return profile.get("legal_name") or profile["name"]


def owner_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    owners: list[dict[str, Any]] = []
    registry = data.get("registry")
    if isinstance(registry, dict) and isinstance(registry.get("owners"), list):
        owners += [dict(o, source=registry["source"]) for o in registry["owners"]]
    gleif = data.get("gleif")
    if isinstance(gleif, dict):
        for relation, parent in (gleif.get("parents") or {}).items():
            owners.append({"name": parent["name"], "country": parent["country"], "share": None, "kind": relation, "source": "GLEIF"})
    ted = data.get("ted_owner_nationality")
    if isinstance(ted, dict):
        for code, notices in (ted.get("nationalities") or {}).items():
            owners.append({"name": f"(owner nationality stated in {len(notices)} TED award notice(s))", "country": ISO3_TO_ISO2_EXTRA.get(code, src.ISO3_TO_ISO2.get(code, code)), "share": None, "kind": "owner nationality", "source": "TED"})
    wiki = data.get("wikidata")
    if isinstance(wiki, dict):
        for parent in wiki.get("parents", []) + wiki.get("owners", []):
            owners.append({"name": parent["name"], "country": parent["country"], "share": None, "kind": "parent/owner", "source": "Wikidata"})
    return owners


# --------------------------------------------------------------------------- scoring


def score_supplier(profile: dict[str, Any], data: dict[str, Any], documents: list[dict[str, Any]], rates: dict[str, float]) -> dict[str, Any]:
    cats = {key: Category(key) for key in WEIGHTS}
    gates: list[dict[str, str]] = []
    registry = data["registry"] if isinstance(data["registry"], dict) and "error" not in data["registry"] else None
    gleif = data["gleif"] if isinstance(data["gleif"], dict) and "lei" in data["gleif"] else None
    vies = data["vies"] if isinstance(data["vies"], dict) and "valid" in data["vies"] else None
    wiki = data["wikidata"] if isinstance(data["wikidata"], dict) and "qid" in data["wikidata"] else None
    track = profile.get("track_record") or {}

    score_identity(cats["identity"], profile, registry, gleif, vies, wiki, data, gates)
    score_ownership(cats["ownership"], data, registry, gates)
    annual_eur = score_track_record(cats["track_record"], profile, track, rates)
    score_financial(cats["financial"], profile, registry, annual_eur, rates)
    score_defence(cats["defence_credentials"], profile, registry, wiki, data, track)
    score_integrity(cats["integrity"], data, registry, gates)
    apply_documents(cats, documents)
    check_sanctions_gate(data, gates)

    available = {k: c.score() for k, c in cats.items() if c.score() is not None}
    weight_with_data = sum(WEIGHTS[k] for k in available)
    mean = sum(WEIGHTS[k] * s for k, s in available.items()) / weight_with_data if weight_with_data else 0.0
    raw_total = round(1 + 9 * mean / 10, 1)
    coverage = weight_with_data + (0.1 if registry else 0.0)
    confidence = "high" if coverage >= 0.85 else "medium" if coverage >= 0.6 else "low"
    caps = []
    total = raw_total
    cap = {"low": 6.0, "medium": 8.5}.get(confidence)
    if cap is not None and total > cap:
        caps.append(f"Capped at {cap} because confidence is {confidence} (only {weight_with_data:.0%} of the weight has data).")
        total = cap
    failed = [g for g in gates if g["status"] == "fail"]
    if failed:
        caps.append("Set to 1.0 by a failed hard gate: " + "; ".join(g["check"] for g in failed) + ".")
        total = 1.0
    # The verdict describes how strong the evidence is; red flags are listed separately so a
    # thin file ("little open data") is not confused with a bad one ("negative findings").
    verdict = (
        "Blocked — manual review required" if failed
        else "Weak evidence" if total < 4
        else "Limited evidence" if total < 6 or confidence == "low"
        else "Strong" if total >= 8 else "Good"
    )
    red_flags = [f"{LABELS[k]}: {flag}" for k, c in cats.items() for flag in c.red_flags]
    return {
        "method_version": METHOD_VERSION,
        "assessed_at": src.today(),
        "total_score": total,
        "score_before_caps": raw_total,
        "verdict": verdict,
        "confidence": confidence,
        "data_coverage": round(weight_with_data, 2),
        "red_flags": red_flags,
        "caps_applied": caps,
        "gates": gates,
        "categories": {k: c.output() for k, c in cats.items()},
        "supplier_documents": len(documents),
        "sanctions_lists_loaded": data["screening"]["lists"],
        "disclaimer": DISCLAIMER,
    }


def score_identity(cat: Category, profile, registry, gleif, vies, wiki, data, gates) -> None:
    name = legal_name(profile)
    founded = ""
    if registry:
        active = registry.get("status_active")
        cat.evidence.append(src.evidence(registry["source"], registry["url"], registry["observed_at"], f"{registry['name']} — {registry.get('status_text')}"))
        if active is False:
            gates.append({"check": "Company active", "status": "fail", "detail": f"{registry['source']}: {registry.get('status_text')}"})
            cat.forced = 0.0
            cat.add(0, f"Register shows the company as {registry.get('status_text')}")
            return
        cat.add(4, f"Found in {registry['source']}, status {registry.get('status_text') or 'active'}")
        similarity = src.name_similarity(name, registry.get("name", ""))
        if similarity >= 0.6:
            cat.add(1.5, "Registered name matches the TED award name")
        else:
            cat.flag(0, f"Registered name '{registry.get('name')}' differs from the TED award name — check the identifier")
        founded = registry.get("founded") or ""
        gates.append({"check": "Company active", "status": "pass", "detail": registry["source"]})
    elif isinstance(data["registry"], dict) and "error" in data["registry"]:
        cat.note(f"(National register unreachable: {data['registry']['error']})")
    if vies:
        cat.evidence.append(src.evidence("VIES (EU VAT)", vies["url"], vies["observed_at"], f"VAT {vies['vat']} valid={vies['valid']}"))
        if vies["valid"]:
            cat.add(1 if registry else 3, f"EU VAT number {vies['vat']} is valid")
            if data.get("ted_identifier_invalid"):
                cat.note(f"(TED lists identifier {data['ted_identifier_invalid']}, which is not a valid VAT number; matched via GLEIF instead — a data error in the award notice, not counted against the supplier)")
        elif gleif:
            cat.note(f"(Identifier {vies['vat']} from TED is not a valid VAT number; the company was matched by name in GLEIF instead)")
        elif re.match(r"^[A-Z]{2}", (profile.get("identifier") or "").upper()):
            cat.flag(0, f"VAT number {vies['vat']} is not valid in VIES and no other register matched")
        else:
            # TED often gives a national register number (KvK, CRO, matična...), not a VAT number.
            cat.note(f"(Identifier {profile.get('identifier')} looks like a national register number, not a VAT number; no free register for this country — ask for a register extract)")
    if gleif:
        cat.evidence.append(src.evidence("GLEIF", gleif["url"], gleif["observed_at"], f"LEI {gleif['lei']} {gleif['registration_status']}, matched by {gleif['match']}"))
        if gleif["registration_status"] == "ISSUED" and gleif["entity_status"] == "ACTIVE":
            cat.add(1.5 if registry or vies else 4, f"Has an active LEI ({gleif['lei']})")
        else:
            cat.add(0.5 if registry or vies else 2, f"LEI {gleif['lei']} is {gleif['registration_status'].lower()} (not renewed)")
        founded = founded or gleif.get("founded", "")
    if not founded and wiki:
        founded = wiki.get("inception", "")
    age = years_between(founded) if founded else None
    if age is not None:
        if age >= 10:
            cat.add(2, f"Established {founded[:4]} ({age:.0f} years)")
        elif age >= 5:
            cat.add(1.5, f"Established {founded[:4]} ({age:.0f} years)")
        elif age >= 2:
            cat.add(0.5, f"Young company, established {founded[:4]}")
        else:
            cat.flag(0, f"Very young company, established {founded[:10]}")
    if not (registry or vies or gleif):
        if profile.get("identifier"):
            cat.add(1, f"Identifier {profile['identifier']} could not be confirmed in any free register — check it")
        else:
            cat.add(1, "No company identifier in the TED award, and no free register match")


def score_ownership(cat: Category, data, registry, gates) -> None:
    owners = owner_list(data)
    if not owners:
        if registry and registry.get("owners") == []:
            cat.note("(Register lists no current shareholders)")
        gates.append({"check": "No Russian/Belarusian control (Reg. 833/2014 Art. 5k)", "status": "not_checked", "detail": "Ownership not available from free sources"})
        return
    countries = Counter(o.get("country") or "?" for o in owners)
    risky = [o for o in owners if (o.get("country") or "").upper() in HIGH_RISK_OWNER_COUNTRIES]
    non_allied = [o for o in owners if (o.get("country") or "").upper() in NON_ALLIED_OWNER_COUNTRIES]
    sources = sorted({o["source"] for o in owners})
    cat.add(4, f"Ownership disclosed in {', '.join(sources)} ({len(owners)} owner/parent records)")
    for owner in owners:
        label = owner["name"] if owner.get("kind") != "person" else f"natural person ({owner.get('country') or '?'})"
        share = f", {owner['share']}" if owner.get("share") else ""
        cat.evidence.append(src.evidence(owner["source"], "", data.get("gleif", {}).get("observed_at", src.today()) if isinstance(data.get("gleif"), dict) else src.today(), f"{owner.get('kind')}: {label} [{owner.get('country') or '?'}]{share}"))
    if risky:
        gates.append({"check": "No Russian/Belarusian control (Reg. 833/2014 Art. 5k)", "status": "fail", "detail": ", ".join(o["name"] for o in risky)})
        cat.forced = 0.0
        cat.flag(0, "Owner or parent registered in Russia/Belarus")
        return
    gates.append({"check": "No Russian/Belarusian control (Reg. 833/2014 Art. 5k)", "status": "pass", "detail": f"Owner countries: {dict(countries)}"})
    known = [o for o in owners if o.get("country") and o["country"] != "?"]
    if known and all(o["country"].upper() in src.ISO3_TO_ISO2.values() or o["country"].upper() in ("US", "CA", "NO", "GB", "TR", "IS") for o in known):
        cat.add(3, "All owners with a known country are in the EU/NATO area")
    elif known:
        cat.add(1, "Some owners are outside the EU/NATO area")
    else:
        cat.add(1, "Owner countries not stated")
    if non_allied:
        cat.flag(-4, "Owner in a non-allied jurisdiction: " + ", ".join(f"{o['name']} ({o['country']})" for o in non_allied))
    if any(o["kind"] in ("ultimate-parent", "direct-parent", "parent/owner") for o in owners):
        cat.add(1.5, "Parent group identified (GLEIF/Wikidata)")
    if any(o["kind"] == "ubo" for o in owners) or (registry or {}).get("extra", {}).get("ubo_count"):
        cat.add(1.5, "Beneficial owners published in a public register")
    if (registry or {}).get("extra", {}).get("rpvs_registered"):
        cat.add(0.5, "Registered in the Slovak Register of Public Sector Partners (verified UBOs)")


def score_track_record(cat: Category, profile, track, rates) -> float | None:
    lots = track.get("lots_won") or 0
    if not lots:
        cat.note("(No awarded lots in the TED profile)")
        return None
    contracts = profile.get("contracts") or []
    buyers = track.get("distinct_buyers") or len({c.get("buyer") for c in contracts})
    countries = len(track.get("buyer_countries") or {})
    cat.add(4 if lots >= 20 else 3 if lots >= 5 else 2, f"{lots} lots won on TED ({track.get('first_award', '?')} – {track.get('last_award', '?')})")
    cat.add(2 if buyers >= 5 else 1 if buyers >= 2 else 0.5, f"{buyers} distinct buyer(s)")
    if countries >= 2:
        cat.add(1, f"Trusted by buyers in {countries} countries")
    defence = sum(1 for c in contracts if DEFENCE_BUYER_WORDS.search(c.get("buyer", "")))
    if defence:
        cat.add(1, f"{defence} lots for defence/police/interior buyers")
    received = [c.get("tenders_received") for c in contracts if isinstance(c.get("tenders_received"), (int, float))]
    if received:
        competitive = sum(1 for r in received if r >= 2) / len(received)
        if competitive >= 0.5:
            cat.add(1.5, f"{competitive:.0%} of wins were contested (2+ bids)")
        elif competitive >= 0.2:
            cat.add(0.75, f"Only {competitive:.0%} of wins were contested")
        else:
            cat.flag(0, f"{1 - competitive:.0%} of wins had a single bid — no proof it beat competitors (a standard procurement red flag)")
    last = track.get("last_award") or ""
    since_last = years_between(last) if last else None
    if since_last is not None and since_last <= 1:
        cat.add(0.5, "Active in the last 12 months")
    span = years_between(track.get("first_award") or "", track.get("last_award") or None) or 0
    total_eur = to_eur(track.get("awarded_value") or {}, rates)
    annual = total_eur / max(1.0, span) if total_eur else None
    ceiling = SIZE_REVENUE_CEILING_EUR.get((profile.get("company_size") or "").lower())
    if annual and ceiling and annual > ceiling:
        cat.flag(-2, f"Awards average €{annual / 1e6:.1f}M/year, above the typical revenue of a '{profile['company_size']}' company — check capacity")
    for contract in contracts[:3]:
        cat.evidence.append(src.evidence("TED award notice", contract.get("ted_url", ""), contract.get("published", ""), f"{contract.get('buyer')}: {contract.get('title', '')[:90]}"))
    return annual


def score_financial(cat: Category, profile, registry, annual_eur, rates) -> None:
    fin = (registry or {}).get("financials") or {}
    filed = (registry or {}).get("filings_last_year")
    this_year = date.today().year
    if filed:
        lag = this_year - filed
        if lag <= 2:
            cat.add(3, f"Latest filed annual accounts: {filed}")
        else:
            cat.flag(1, f"Latest filed annual accounts: {filed} (overdue)")
        cat.evidence.append(src.evidence(registry["source"], registry["url"], registry["observed_at"], f"Annual accounts filed for {filed}"))
    revenue = fin.get("revenue")
    if revenue:
        revenue_eur = revenue / rates.get(fin.get("currency") or "EUR", 1.0)
        cat.add(2, f"Revenue {fin['year']}: €{revenue_eur / 1e6:.1f}M")
        profit = fin.get("net_profit")
        if profit is not None:
            amount = f"€{profit / rates.get(fin.get('currency') or 'EUR', 1.0) / 1e6:.1f}M"
            if profit >= 0:
                cat.add(2, f"Net profit {amount}")
            else:
                cat.flag(0, f"Net loss {amount}")
        equity = fin.get("equity")
        if equity is not None:
            if equity > 0:
                cat.add(1, "Positive equity")
            else:
                cat.flag(-2, "Negative equity")
        if annual_eur:
            ratio = annual_eur / revenue_eur if revenue_eur else 99
            if ratio <= 0.5:
                cat.add(2, f"TED awards ≈ {ratio:.0%} of annual revenue — within capacity")
            elif ratio <= 1:
                cat.add(1, f"TED awards ≈ {ratio:.0%} of annual revenue")
            else:
                cat.flag(0, f"TED awards ≈ {ratio:.0%} of annual revenue — exceeds turnover, check capacity")
        else:
            cat.add(1, "(No award value to compare with revenue)")
    elif fin.get("employees"):
        # Headcount alone says nothing about financial health; keep it as context only.
        cat.note(f"({fin['employees']:.0f} employees reported; no published accounts)")
    if registry and registry.get("extra", {}).get("registered_arrears"):
        cat.flag(-2, "Registered arrears/claims in the court register")


def score_defence(cat: Category, profile, registry, wiki, data, track) -> None:
    codes = [re.sub(r"\D", "", c) for c in (registry or {}).get("activity_codes", []) if c]
    texts = (registry or {}).get("activity_text", [])
    if registry and (codes or texts):
        if any(code.startswith(DEFENCE_NACE) for code in codes):
            cat.add(4, "Registered activity code is weapons/ammunition/military manufacturing (NACE 25.40 or similar)")
        licence = [t for t in texts if DEFENCE_WORDS.search(t)]
        if licence:
            cat.add(4, "Register lists a licensed weapons/military-material activity: " + licence[0][:110])
        if not licence and not any(code.startswith(DEFENCE_NACE) for code in codes):
            cat.flag(1, f"Registered activity ({', '.join(codes[:3]) or texts[0][:40]}) is not defence-specific — trading company?")
        cat.evidence.append(src.evidence(registry["source"], registry["url"], registry["observed_at"], f"Activity codes {codes[:5]}"))
    if wiki and any(DEFENCE_WORDS.search(i) or "arms" in i.lower() for i in wiki.get("industries", [])):
        cat.add(2, f"Wikidata industry: {', '.join(wiki['industries'][:3])}")
        cat.evidence.append(src.evidence("Wikidata", wiki["url"], wiki["observed_at"], f"industry {wiki['industries'][:3]}"))
    parent = data.get("parent_wikidata") if isinstance(data.get("parent_wikidata"), dict) and "qid" in data["parent_wikidata"] else None
    if parent and any(DEFENCE_WORDS.search(i) or "arms" in i.lower() for i in parent.get("industries", [])):
        cat.add(2, f"Parent group is a defence manufacturer (Wikidata: {', '.join(parent['industries'][:2])})")
        cat.evidence.append(src.evidence("Wikidata (parent via GLEIF)", parent["url"], parent["observed_at"], f"parent industry {parent['industries'][:3]}"))
    funding = data.get("eu_funding")
    if isinstance(funding, dict) and "organisations" in funding:
        if funding["defence_projects"]:
            cat.add(2.5, f"Named in {len(funding['defence_projects'])} EU defence programme project(s) (EDF/ASAP/EDIRPA)")
            for project in funding["defence_projects"][:3]:
                cat.evidence.append(src.evidence("EU Funding & Tenders portal", project["url"], funding["observed_at"], f"{project['call']}: {project['title'][:80]}"))
        if funding["organisations"]:
            cat.add(0.5, "Validated EU participant (PIC) in the Funding & Tenders portal")
    weapons_lots = sum(1 for c in profile.get("contracts") or [] if any(code.startswith("353") for code in c.get("cpv_codes", [])))
    # Weapons awards alone are track record, not a credential: without any credential source the
    # category is "no data" (lowers confidence) rather than a low score.
    if weapons_lots and cat.has_data:
        cat.add(1, f"{weapons_lots} weapons/ammunition (CPV 353) lots delivered to public buyers")
    elif weapons_lots:
        cat.note(f"({weapons_lots} weapons/ammunition lots on TED, but no free source for licences or industry codes in this country)")


def score_integrity(cat: Category, data, registry, gates) -> None:
    screening = data["screening"]
    lists = ", ".join(sorted(screening["lists"])) or "none loaded"
    if not screening["lists"]:
        cat.note("(Sanctions lists could not be loaded)")
        return
    cat.add(10, f"Sanctions screening against {lists}")
    close = {e["name"] for result in screening["company_and_owner_names"].values() for e in result["close"]}
    if close:
        cat.flag(-3, f"Similar (not identical) names on sanctions lists — review: {', '.join(sorted(close)[:3])}")
    persons = screening["persons"]
    if persons:
        cat.note(f"({len(persons)} named officers screened; names are not stored in this report)")
    media = data.get("media")
    if isinstance(media, dict) and "articles" in media:
        hits = media["articles"]
        if hits:
            cat.flag(-1 if len(hits) <= 2 else -2, f"{len(hits)} recent news headline(s) pairing the name with fraud/corruption/sanctions terms — unverified press, review")
            for article in hits[:3]:
                cat.evidence.append(src.evidence("GDELT news", article["url"], media["observed_at"], article["title"][:120], "inferred"))
        else:
            cat.note("(No adverse headlines in GDELT's last ~3 months)")
    elif isinstance(media, dict):
        cat.note(f"(News check unavailable: {media.get('error')})")
    extra = (registry or {}).get("extra", {})
    if extra.get("suspensions"):
        cat.flag(-5, "Register shows activity suspension/prohibition: " + "; ".join(extra["suspensions"][:2]))
        gates.append({"check": "Activity not suspended", "status": "fail", "detail": "; ".join(extra["suspensions"][:2])})
    if registry and registry.get("insolvency"):
        gates.append({"check": "No insolvency/liquidation", "status": "fail", "detail": registry["source"]})
        cat.flag(-5, "Insolvency, liquidation or restructuring registered")
    elif registry and registry.get("insolvency") is False:
        gates.append({"check": "No insolvency/liquidation", "status": "pass", "detail": registry["source"]})
    else:
        gates.append({"check": "No insolvency/liquidation", "status": "not_checked", "detail": "No free insolvency source for this country"})
    if extra.get("ubo_public_officials"):
        cat.note(f"({extra['ubo_public_officials']} beneficial owner(s) flagged as public officials in RPVS)")


def check_sanctions_gate(data, gates) -> None:
    hits = []
    for name, result in list(data["screening"]["company_and_owner_names"].items()) + list(data["screening"]["persons"].items()):
        for entry in result["exact"]:
            hits.append(f"{entry['list']} {entry['ref']} ({entry['kind']})")
    gates.append({
        "check": "No sanctions match (EU, UN, OFAC, UK)",
        "status": "fail" if hits else ("pass" if data["screening"]["lists"] else "not_checked"),
        "detail": "Exact name match, verify identity: " + "; ".join(hits[:5]) if hits else f"Screened company, owners and officers against {', '.join(sorted(data['screening']['lists']))}",
    })


def apply_documents(cats: dict[str, Category], documents: list[dict[str, Any]]) -> None:
    for doc in documents:
        kind = doc.get("type", "")
        if kind not in DOCUMENT_POINTS:
            continue
        category, points, label = DOCUMENT_POINTS[kind]
        factor, status = document_status(doc)
        cat = cats[category]
        if factor and points and not cat.has_data:
            # No open data for this category: start from neutral so documents can only help.
            cat.add(5, "(No open data for this category; scored from supplier documents, starting at a neutral 5)")
        cat.evidence.append(src.evidence("Supplier-provided document", doc.get("file", ""), doc.get("uploaded_at", src.today()), f"{label}: {doc.get('title') or doc.get('number') or ''}", "document_verified" if factor == 1.0 else "declared"))
        if factor == 0:
            cat.note(f"({label} — {status}, not counted)")
            continue
        if kind == "financial_statements" and not cats["financial"].has_data and doc.get("revenue"):
            profit = doc.get("net_profit")
            cat.add(4 * factor, f"Supplier-provided accounts {doc.get('year', '')}: revenue {doc['revenue']:,} {doc.get('currency', '')} ({status})")
            if profit is not None:
                cat.add((2 if profit >= 0 else 0) * factor, f"Net {'profit' if profit >= 0 else 'loss'} {profit:,} {doc.get('currency', '')}")
        elif kind == "ubo_declaration" and not cats["ownership"].has_data and doc.get("owners"):
            countries = {o.get("country", "") for o in doc["owners"]}
            if countries & HIGH_RISK_OWNER_COUNTRIES:
                cat.forced = 0.0
                cat.add(0, "Declared owner in Russia/Belarus")
            else:
                cat.add(6 * factor, f"Supplier declared {len(doc['owners'])} owner(s) in {', '.join(sorted(countries))} ({status})")
        elif points:
            cat.add(points * factor, f"{label} ({status})")


# --------------------------------------------------------------------------- KYC (K1-K11)
# Statuses: pass · fail · review (a person must look) · needs_upload (supplier must provide)
#           · pending_platform (checked during onboarding on our platform) · info


def kyc_item(title: str, gate: str, status: str, summary: str, sources: list[str], detail: Any = None) -> dict[str, Any]:
    item = {"title": title, "gate": gate, "status": status, "summary": summary, "sources": sources}
    if detail:
        item["detail"] = detail
    return item


def gate_status(dd: dict[str, Any], check_prefix: str) -> dict[str, str] | None:
    return next((g for g in dd["gates"] if g["check"].startswith(check_prefix)), None)


def best_document(documents: list[dict[str, Any]], kinds: tuple[str, ...]) -> tuple[dict[str, Any] | None, str]:
    """Return the strongest valid document of these kinds and its status label."""
    ranked = []
    for doc in documents:
        if doc.get("type") in kinds:
            factor, label = document_status(doc)
            ranked.append((factor, doc, label))
    if not ranked:
        return None, ""
    factor, doc, label = max(ranked, key=lambda r: r[0])
    return doc, label


def build_kyc(profile: dict[str, Any], data: dict[str, Any], dd: dict[str, Any], documents: list[dict[str, Any]]) -> dict[str, Any]:
    registry = data["registry"] if isinstance(data["registry"], dict) and "error" not in data["registry"] else None
    gleif = data["gleif"] if isinstance(data["gleif"], dict) and "lei" in data["gleif"] else None
    vies = data["vies"] if isinstance(data["vies"], dict) and "valid" in data["vies"] else None
    reg_source = registry["source"] if registry else ""
    items: dict[str, dict[str, Any]] = {}

    # K1 legal identity
    active_gate = gate_status(dd, "Company active")
    if active_gate and active_gate["status"] == "fail":
        items["K1"] = kyc_item("Legal identity: company exists and is active", "HARD", "fail", active_gate["detail"], [reg_source])
    elif registry:
        items["K1"] = kyc_item("Legal identity: company exists and is active", "HARD", "pass",
                               f"{registry['name']} — {registry.get('status_text') or 'active'}, founded {registry.get('founded') or '?'}; {registry.get('address', '')}".strip("; "),
                               [reg_source] + (["VIES"] if vies else []) + (["GLEIF"] if gleif else []))
    elif (vies and vies["valid"]) or (gleif and gleif["entity_status"] == "ACTIVE"):
        what = []
        if vies and vies["valid"]:
            what.append(f"VAT {vies['vat']} valid")
        if gleif:
            what.append(f"LEI {gleif['lei']} entity {gleif['entity_status'].lower()}, registration {gleif['registration_status'].lower()}")
        items["K1"] = kyc_item("Legal identity: company exists and is active", "HARD", "pass",
                               "EU-level sources only (no free national register adapter): " + "; ".join(what),
                               (["VIES"] if vies else []) + (["GLEIF"] if gleif else []))
    else:
        items["K1"] = kyc_item("Legal identity: company exists and is active", "HARD", "needs_upload",
                               "Not found in any free register — upload a current register extract", [])

    # K2 insolvency
    insolvency = gate_status(dd, "No insolvency")
    status = {"fail": "fail", "pass": "pass"}.get(insolvency["status"] if insolvency else "", "needs_upload")
    items["K2"] = kyc_item("No bankruptcy, liquidation or restructuring", "HARD", status,
                           {"fail": f"Registered in {reg_source}", "pass": f"Nothing registered in {reg_source}"}.get(status, "No free insolvency source for this country — upload a register extract (< 3 months old)"),
                           [reg_source] if registry else [])

    # K3 authority to sign
    reps = (registry or {}).get("representatives") or []
    if reps:
        items["K3"] = kyc_item("Person signing the agent mandate may represent the company", "HARD", "pending_platform",
                               f"{len(reps)} registered representative(s); rule: {(registry.get('representation_rule') or 'not stated')[:200]}. Match the signer at onboarding, or require a power of attorney.",
                               [reg_source], [{"name": r["name"], "role": r["role"]} for r in reps])
    else:
        items["K3"] = kyc_item("Person signing the agent mandate may represent the company", "HARD", "needs_upload",
                               "Representatives not available from free sources — upload a register extract or power of attorney", [])

    # K4 ownership
    rb = gate_status(dd, "No Russian/Belarusian")
    owners = owner_list(data)
    ubo_doc, ubo_label = best_document(documents, ("ubo_declaration",))
    if rb and rb["status"] == "fail":
        k4 = ("fail", "Russian/Belarusian owner or parent: " + rb["detail"])
    elif owners:
        non_allied = [o for o in owners if (o.get("country") or "").upper() in NON_ALLIED_OWNER_COUNTRIES]
        k4 = ("review" if non_allied else "pass", f"{len(owners)} owner/parent record(s); countries: {sorted({o.get('country') or '?' for o in owners})}")
    elif ubo_doc:
        k4 = ("review" if "declared" in ubo_label else "pass", f"Ownership from supplier declaration ({ubo_label})")
    else:
        k4 = ("needs_upload", "Ownership not public for this country — upload a shareholder / beneficial-owner declaration")
    items["K4"] = kyc_item("Ownership and non-EU control", "SCORE (HARD if sanctioned owner)", k4[0], k4[1],
                           sorted({o["source"] for o in owners}),
                           [{"name": o["name"] if o.get("kind") != "person" else "natural person", "country": o.get("country"), "share": o.get("share"), "kind": o.get("kind"), "source": o.get("source")} for o in owners])

    # K5 sanctions and debarment
    sanctions = gate_status(dd, "No sanctions")
    close = any("Similar (not identical)" in f for f in dd["categories"]["integrity"]["findings"])
    k5 = sanctions["status"] if sanctions else "not_checked"
    k5 = "review" if k5 == "pass" and close else k5
    items["K5"] = kyc_item("Sanctions and debarment: company, owners, board", "HARD", k5,
                           (sanctions["detail"] if sanctions else "Sanctions lists not loaded") + ". Debarment lists (EDES, World Bank) not covered by free APIs.",
                           sorted(dd["sanctions_lists_loaded"]))

    # K6 VAT and payout account
    accounts = (registry or {}).get("extra", {}).get("registered_bank_accounts") or []
    if vies and vies["valid"]:
        summary = f"VAT {vies['vat']} active in VIES."
        summary += f" {len(accounts)} bank account(s) registered with the tax office — the payout account must be one of them." if accounts else " Payout account ownership to be checked at onboarding."
        items["K6"] = kyc_item("Active EU VAT; payout account belongs to the company", "HARD for payouts", "pass", summary,
                               ["VIES"] + (["PL VAT white list"] if accounts else []), {"registered_bank_accounts": accounts} if accounts else None)
    elif profile["country"] in ("GBR", "ZAF", "NOR", "CHE"):
        items["K6"] = kyc_item("Active EU VAT; payout account belongs to the company", "HARD for payouts", "needs_upload", "Non-EU company: no VIES; upload tax registration and bank confirmation", [])
    else:
        items["K6"] = kyc_item("Active EU VAT; payout account belongs to the company", "HARD for payouts", "review", "No valid EU VAT number confirmed — ask for the VAT number", ["VIES"])

    # K7 sector licence (HARD for the four weapons/ammunition CPV classes)
    in_scope = any(c.startswith("353") for c in (x["code"] for x in profile.get("top_cpv", [])))
    licence_doc, licence_label = best_document(documents, ("arms_trade_licence", "export_licence", "certider"))
    # Only wording that names a permit/licence/concession counts; an activity description
    # ("manufacture of weapons") is a NACE code, not proof of a licence.
    register_licence = [t for t in (registry or {}).get("activity_text", []) if DEFENCE_WORDS.search(t) and LICENCE_WORDS.search(t)]
    defence_activity = [t for t in (registry or {}).get("activity_text", []) if DEFENCE_WORDS.search(t)]
    defence_code = bool(defence_activity) or any(re.sub(r"\D", "", c).startswith(DEFENCE_NACE) for c in (registry or {}).get("activity_codes", []))
    if licence_doc and "verified" in licence_label:
        k7 = ("pass", f"{licence_doc.get('title') or licence_doc['type']} ({licence_label})")
    elif licence_doc and "declared" in licence_label:
        k7 = ("review", f"{licence_doc.get('title') or licence_doc['type']} uploaded — verify with the issuing authority")
    elif register_licence:
        k7 = ("pass", f"Licensed activity recorded in {reg_source}: {register_licence[0][:150]}")
    elif defence_code:
        k7 = ("needs_upload", f"Register shows a weapons/ammunition activity ({(defence_activity or ['NACE 25.40'])[0][:80]}), but the licence itself is not in the register — upload it")
    else:
        k7 = ("needs_upload", "No licence visible in free registers — upload the home-state licence (e.g. PL MSWiA concession, DE WaffG/KrWaffKontrG permit, FR AFC authorisation)")
    items["K7"] = kyc_item("Home-state licence to make or trade weapons and ammunition", "HARD" if in_scope else "SCORE", k7[0], k7[1], [reg_source] if register_licence or defence_code else [])

    # K8 quality certificates
    cert, cert_label = best_document(documents, ("iso_9001", "en_9100"))
    items["K8"] = kyc_item("Quality certificates (ISO 9001, EN 9100) with expiry", "SCORE",
                           ("pass" if "verified" in cert_label else "review" if cert_label.startswith("declared") else "fail" if cert_label.startswith("expired") else "needs_upload"),
                           f"{cert.get('title') or cert['type']}, valid until {cert.get('valid_until', '?')} ({cert_label})" if cert else "Upload certificate; verify on IAF CertSearch / IAQG OASIS",
                           [])

    # K9 track record
    track = profile.get("track_record") or {}
    items["K9"] = kyc_item("Track record: past public contracts", "SCORE", "pass" if track.get("lots_won") else "info",
                           f"{track.get('lots_won', 0)} lots from {track.get('distinct_buyers', 0)} buyer(s) in {len(track.get('buyer_countries') or {})} country(ies), {track.get('first_award', '?')} – {track.get('last_award', '?')}; track-record score {dd['categories']['track_record']['score']}",
                           ["TED"])

    # K10 capability baseline
    items["K10"] = kyc_item("Capabilities: NACE code, CPVs, declared capacity", "INFO", "info",
                            f"Register activity codes {(registry or {}).get('activity_codes', [])[:6]}; TED CPVs {[c['code'] for c in profile.get('top_cpv', [])][:6]}"
                            + (f"; {len(profile['catalogue'])} catalogue items" if isinstance(profile.get("catalogue"), list) else "")
                            + "; declared capacity: not provided",
                            ([reg_source] if registry else []) + ["TED"])

    # K11 agent mandate
    items["K11"] = kyc_item("Agent mandate signed: limits, categories, escalation contact", "HARD", "pending_platform",
                            "Signed on the platform during onboarding", ["Platform"])

    hard_fail = [k for k, v in items.items() if v["status"] == "fail" and v["gate"].startswith("HARD")]
    open_items = [f"{k}: {v['status']}" for k, v in items.items() if v["status"] not in ("pass", "info")]
    return {
        "kyc_status": "blocked" if hard_fail or dd["verdict"].startswith("Blocked") else ("verified" if not open_items else "limited"),
        "kyc_checked_at": dd["assessed_at"],
        "refresh_due": (date.fromisoformat(dd["assessed_at"]) + timedelta(days=KYC_REFRESH_DAYS)).isoformat(),
        "open_items": open_items,
        "checks": items,
    }


# --------------------------------------------------------------------------- deal baseline
# Per-deal checks (D1-D14) run inside each deal, but some inputs are supplier facts that can be
# computed once from open data and kept on the profile.


def build_deal_baseline(profile: dict[str, Any], data: dict[str, Any], rates: dict[str, float]) -> dict[str, Any]:
    contracts = profile.get("contracts") or []
    lots = []
    for contract in contracts:
        value = contract.get("awarded_value") or {}
        rate = rates.get(value.get("currency") or "EUR")
        if value.get("value") and rate:
            lots.append((value["value"] / rate, contract))
    lots.sort(key=lambda item: item[0])
    registry = data["registry"] if isinstance(data["registry"], dict) and "error" not in data["registry"] else None
    fin = (registry or {}).get("financials") or {}
    revenue_eur = fin["revenue"] / rates.get(fin.get("currency") or "EUR", 1.0) if fin.get("revenue") else None

    by_cpv: dict[str, list[float]] = {}
    for value, contract in lots:
        for code in contract.get("cpv_codes") or []:
            by_cpv.setdefault(code, []).append(value)
    price_baseline = {
        code: {"lots": len(values), "median_lot_eur": round(sorted(values)[len(values) // 2]), "min_lot_eur": round(min(values)), "max_lot_eur": round(max(values))}
        for code, values in by_cpv.items()
    }
    largest = lots[-1] if lots else None
    track = profile.get("track_record") or {}
    modifications = sum(1 for c in contracts if c.get("notice_type") == "can-modif")
    activity_text = " ".join((registry or {}).get("activity_text", []))
    trader = bool(registry) and not any(re.sub(r"\D", "", c).startswith(DEFENCE_NACE) for c in registry.get("activity_codes", [])) and not DEFENCE_WORDS.search(activity_text)
    return {
        "D1_freshness": {"kyc_checked_at": src.today(), "sanctions_screened_at": src.today(), "rescreen_every_deal": True},
        "D6_capacity_hints": {
            "largest_lot_eur": round(largest[0]) if largest else None,
            "largest_lot": (largest[1].get("title") or "")[:120] if largest else None,
            "largest_lot_quantity": largest[1].get("quantity") if largest else None,
            "framework_lots": track.get("framework_agreement_lots"),
            "note": "Largest past delivery on TED; certificates do not prove free capacity — ask for a dated capacity statement.",
        },
        "D9_price_baseline_by_cpv": price_baseline,
        "D9_note": "Lot values, not unit prices; framework lots are ceilings. Compare offers against lots in the same CPV.",
        "D10_financial_capacity": {
            "revenue_eur": round(revenue_eur) if revenue_eur else None,
            "revenue_year": fin.get("year"),
            "employees": fin.get("employees"),
            "largest_lot_to_revenue": round(largest[0] / revenue_eur, 2) if largest and revenue_eur else None,
            "company_size_ted": profile.get("company_size"),
        },
        "D12_integrity_signals": {
            "registered_as_trader_not_manufacturer": trader,
            "related_suppliers_same_owner": [],
            "single_bid_share": dd_single_bid_share(contracts),
        },
        "D13_reference_candidates": [
            {"buyer": b["name"], "lots": b["lots"]} for b in (track.get("repeat_buyers") or [])[:5]
        ],
        "D13_contract_modifications_on_ted": modifications,
    }


def dd_single_bid_share(contracts: list[dict[str, Any]]) -> float | None:
    received = [c.get("tenders_received") for c in contracts if isinstance(c.get("tenders_received"), (int, float))]
    return round(sum(1 for r in received if r < 2) / len(received), 2) if received else None


def link_related_parties(profiles: list[dict[str, Any]]) -> None:
    """D12: suppliers in our data set that share a named corporate owner or parent."""
    by_owner: dict[str, set[str]] = {}
    for profile in profiles:
        for owner in ((profile.get("kyc") or {}).get("checks", {}).get("K4", {}).get("detail") or []):
            name = owner.get("name") or ""
            if owner.get("kind") in ("person", "ubo", "owner nationality") or name.startswith("(") or name == "natural person":
                continue
            by_owner.setdefault(src.norm_name(name), set()).add(profile["id"])
    for profile in profiles:
        baseline = profile.get("deal_baseline")
        if not baseline:
            continue
        related = set()
        for owner_ids in by_owner.values():
            if profile["id"] in owner_ids:
                related |= owner_ids - {profile["id"]}
        baseline["D12_integrity_signals"]["related_suppliers_same_owner"] = sorted(related)


# --------------------------------------------------------------------------- CLI


def summary_table(profiles: list[dict[str, Any]]) -> str:
    keys = list(WEIGHTS)
    head = "| Supplier | Country | Total | Verdict | Confidence | " + " | ".join(LABELS[k].split(" (")[0] for k in keys) + " |"
    rows = [head, "|" + "---|" * (5 + len(keys))]
    for p in sorted(profiles, key=lambda p: -(p.get("due_diligence") or {}).get("total_score", 0)):
        dd = p.get("due_diligence")
        if not dd:
            continue
        cells = [("–" if dd["categories"][k]["score"] is None else f"{dd['categories'][k]['score']:.1f}") for k in keys]
        rows.append(f"| {p['name'][:40]} | {p['country']} | **{dd['total_score']:.1f}** | {dd['verdict']} | {dd['confidence']} | " + " | ".join(cells) + " |")
    return "\n".join(rows)


KYC_ICONS = {"pass": "✅", "fail": "❌", "review": "👀", "needs_upload": "📄", "pending_platform": "⏳", "info": "ℹ️", "not_checked": "–"}


def kyc_table(profiles: list[dict[str, Any]]) -> str:
    keys = [f"K{i}" for i in range(1, 12)]
    rows = [
        "| Supplier | KYC status | " + " | ".join(keys) + " |",
        "|" + "---|" * (2 + len(keys)),
    ]
    details = []
    for p in profiles:
        kyc = p.get("kyc")
        if not kyc:
            continue
        cells = [KYC_ICONS.get(kyc["checks"][k]["status"], kyc["checks"][k]["status"]) for k in keys]
        rows.append(f"| {p['name'][:40]} | **{kyc['kyc_status']}** | " + " | ".join(cells) + " |")
        open_lines = [f"  - {k} {kyc['checks'][k]['title']}: {kyc['checks'][k]['status']} — {kyc['checks'][k]['summary'][:160]}" for k in keys if kyc["checks"][k]["status"] not in ("pass", "info")]
        details.append(f"- **{p['name']}** (checked {kyc['kyc_checked_at']}, refresh due {kyc['refresh_due']})\n" + "\n".join(open_lines))
    legend = "Legend: " + " · ".join(f"{icon} {status}" for status, icon in KYC_ICONS.items())
    return "\n".join(rows) + "\n\n" + legend + "\n\n### Open items per supplier\n\n" + "\n".join(details)


def write_summary(data_dir: Path, profiles: list[dict[str, Any]]) -> str:
    table = summary_table(profiles)
    (data_dir / "profiles" / "due_diligence_summary.md").write_text(
        f"# Supplier due diligence — {src.today()} ({METHOD_VERSION})\n\n"
        "## Scores\n\n"
        "Total = weighted mean of category scores (identity 15%, ownership 20%, track record 25%, "
        "financial 15%, defence credentials 15%, integrity 10%), mapped to 1–10. "
        "'–' = no free open data for that category (excluded, lowers confidence).\n\n"
        f"{table}\n\n"
        "## KYC checks K1–K11\n\n"
        "K1 identity · K2 insolvency · K3 authority to sign · K4 ownership · K5 sanctions · K6 VAT & payout · "
        "K7 weapons licence · K8 quality certificates · K9 track record · K10 capabilities · K11 agent mandate. "
        "Full detail per check: `kyc.checks` in `suppliers.json`.\n\n"
        f"{kyc_table(profiles)}\n\n> {DISCLAIMER}\n",
        encoding="utf-8",
    )
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description="Score suppliers 1-10 from free open data (see module docstring for the method).")
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--only", help="score only this supplier id")
    parser.add_argument("--offline", action="store_true", help="use cached source data only (fast re-score after a document upload)")
    parser.add_argument("--no-media", action="store_true", help="skip the GDELT news check (it is rate-limited to 1 request / 5 s)")
    parser.add_argument("--summary-only", action="store_true", help="only rewrite due_diligence_summary.md from suppliers.json")
    args = parser.parse_args()

    src.OFFLINE = args.offline
    profiles_path = args.data_dir / "profiles" / "suppliers.json"
    if args.summary_only:
        print(write_summary(args.data_dir, json.loads(profiles_path.read_text(encoding="utf-8"))))
        return 0
    evidence_dir = args.data_dir / "supplier_evidence"
    profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
    print("Loading sanctions lists (EU, UN, OFAC, UK)...", file=sys.stderr)
    sanctions = src.sanctions_index()
    print(f"  loaded: {sanctions['loaded']}", file=sys.stderr)
    rates = src.ecb_rates()

    for profile in profiles:
        if args.only and profile["id"] != args.only:
            continue
        print(f"Assessing {profile['name']} ({profile['country']})...", file=sys.stderr)
        data = collect(profile, sanctions, with_media=not args.no_media)
        documents = load_documents(evidence_dir, profile["id"])
        profile["due_diligence"] = score_supplier(profile, data, documents, rates)
        profile["kyc"] = build_kyc(profile, data, profile["due_diligence"], documents)
        profile["deal_baseline"] = build_deal_baseline(profile, data, rates)
        dd = profile["due_diligence"]
        print(f"  → {dd['total_score']:.1f}/10  {dd['verdict']}  (confidence {dd['confidence']}), KYC {profile['kyc']['kyc_status']}", file=sys.stderr)
    link_related_parties(profiles)

    # Other scripts edit suppliers.json too: re-read it and merge only the keys this script owns.
    current = json.loads(profiles_path.read_text(encoding="utf-8"))
    ours = {p["id"]: p for p in profiles}
    for profile in current:
        for key in ("due_diligence", "kyc", "deal_baseline"):
            if key in ours.get(profile["id"], {}):
                profile[key] = ours[profile["id"]][key]
    profiles_path.write_text(json.dumps(current, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    table = write_summary(args.data_dir, current)
    print(table)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
