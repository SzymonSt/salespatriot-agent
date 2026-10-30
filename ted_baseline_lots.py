#!/usr/bin/env python3
"""Lot-level data for the frozen TED baseline (baseline_v2): fetch notice XML, parse lots, write JSONL.

Reads TED result notices (eForms and pre-eForms TED_EXPORT R2.0.8/R2.0.9 XML) and writes one record per lot
result: lot id, title, short description, CPV codes, quantity and unit (eForms BT-25/BT-625 only), estimated
value, framework flag and maximum, result code, winners paired through the XML's own references (eForms:
LotResult -> LotTender -> TenderingParty -> Organization; legacy: AWARD_CONTRACT -> CONTRACTOR), the lot's own
value and currency, and bids received. Classification (rules-v1, kb_baseline_classify) and strict unit prices
are computed in the database (kb_baseline_lots_classify), not here.

Usage:
  python3 ted_baseline_lots.py --list data/kb_baseline_v2_sample.csv            # fetch (cached) + parse
  python3 ted_baseline_lots.py --list data/kb_baseline_v2_sample.csv --fetch-only
  python3 ted_baseline_lots.py --list ... --out .cache/kb_baseline_v2_lots.jsonl --push   # needs .env

Fetching: at most 1 request per second, retries with exponential backoff, raw XML cached in .cache/ted_xml/
(gitignored, shared with ted_supplier_contracts.py), so reruns are free and an interrupted run resumes.
Raw XML never goes to the database. --push upserts the parsed lots into kb_baseline_lots through PostgREST with
SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (environment or the gitignored .env next to this script).

Sample of baseline_v2 (the list in data/kb_baseline_v2_sample.csv): 250 result notices of baseline_v1 procedures
in the four focus classes, stratified by class in proportion to the result notices per class (ammunition 156,
firearms 45, parts 40, misc_weapons 9), each class split about half pre-eForms / half eForms; within a stratum
ranked by score = 4 x calibre known + 2 x buyer has an open tender + 1 x buyer country has an open tender +
1 x family appears in open tenders (ted_open_tenders on 2026-10-30), then newest first. Plus 5 notices that were
already cached. To extend the baseline later, pass a longer list (or --all-focus-results) to the same script.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import ssl
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ted_supplier_contracts import XML_URL, amount, local, strip_namespaces, text

try:  # hardened parser when available; the stdlib parser (expat >= 2.4) does not load external entities
    from defusedxml.ElementTree import parse as parse_xml  # type: ignore
except ImportError:
    parse_xml = ET.parse

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / ".cache" / "ted_xml"
MIN_INTERVAL_SECONDS = 1.0
MAX_RETRIES = 6
DESCRIPTION_CHARS = 300
TITLE_CHARS = 300

# ISO 3166 alpha-2 (and the TED legacy codes UK, EL) -> alpha-3, as used by eForms and the Search API.
ALPHA3 = {
    "AT": "AUT", "BE": "BEL", "BG": "BGR", "HR": "HRV", "CY": "CYP", "CZ": "CZE", "DK": "DNK", "EE": "EST",
    "FI": "FIN", "FR": "FRA", "DE": "DEU", "GR": "GRC", "EL": "GRC", "HU": "HUN", "IE": "IRL", "IT": "ITA",
    "LV": "LVA", "LT": "LTU", "LU": "LUX", "MT": "MLT", "NL": "NLD", "PL": "POL", "PT": "PRT", "RO": "ROU",
    "SK": "SVK", "SI": "SVN", "ES": "ESP", "SE": "SWE", "GB": "GBR", "UK": "GBR", "NO": "NOR", "IS": "ISL",
    "LI": "LIE", "CH": "CHE", "US": "USA", "CA": "CAN", "IL": "ISR", "TR": "TUR", "UA": "UKR", "RS": "SRB",
    "BA": "BIH", "ME": "MNE", "MK": "MKD", "AL": "ALB", "MD": "MDA", "KR": "KOR", "JP": "JPN", "AU": "AUS",
    "ZA": "ZAF", "BR": "BRA", "AE": "ARE", "SG": "SGP", "IN": "IND", "CN": "CHN", "XK": "XKX", "GE": "GEO",
    "AR": "ARG", "MX": "MEX", "NZ": "NZL", "SA": "SAU", "TW": "TWN", "HK": "HKG", "BY": "BLR", "RU": "RUS",
}


# ----------------------------------------------------------------------------------------------- fetching

_last_request = 0.0


def _ssl_context() -> ssl.SSLContext | None:
    # System python3 on this Mac lacks a CA bundle; certifi's bundle fixes CERTIFICATE_VERIFY_FAILED.
    if os.environ.get("SSL_CERT_FILE"):
        return None
    try:
        import certifi  # type: ignore

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return None


_SSL = _ssl_context()


def fetch_xml(number: str, cache_dir: Path) -> Path | None:
    """Download one notice XML into the cache (1 request/s, retries with backoff). Returns the path or None."""
    global _last_request
    path = cache_dir / f"{number}.xml"
    if path.exists() and path.stat().st_size > 0:
        return path
    request = Request(XML_URL.format(number=number), headers={"User-Agent": "salespatriot-baseline-lots/1.0"})
    for attempt in range(MAX_RETRIES):
        wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            with urlopen(request, timeout=60, context=_SSL) as response:
                body = response.read()
            if not body.lstrip().startswith(b"<"):
                raise ValueError("response is not XML")
            tmp = path.with_suffix(".part")
            tmp.write_bytes(body)
            tmp.replace(path)
            return path
        except HTTPError as error:
            if error.code == 404:
                print(f"  {number}: 404, skipped", file=sys.stderr)
                return None
            reason = f"HTTP {error.code}"
        except (URLError, TimeoutError, ValueError, OSError) as error:
            reason = type(error).__name__
        delay = min(120, 2 ** attempt * 2)
        print(f"  {number}: {reason}, retry {attempt + 1}/{MAX_RETRIES} in {delay}s", file=sys.stderr)
        time.sleep(delay)
    return None


# ----------------------------------------------------------------------------------------------- helpers

def num(value: str | None) -> float | None:
    """Parse '954 576', '1024390.24' or '1.234,56' style numbers; None when empty or unparseable."""
    if value is None:
        return None
    raw = re.sub(r"[\s ]", "", value)
    if not raw:
        return None
    if "," in raw and "." in raw:
        raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def alpha3(code: str | None) -> str | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    return code if len(code) == 3 else ALPHA3.get(code, code)


def paragraphs(element: ET.Element | None) -> str:
    if element is None:
        return ""
    parts = [" ".join((p.text or "").split()) for p in element.iter("P")]
    joined = " ".join(p for p in parts if p)
    return joined or " ".join((element.text or "").split())


def clip(value: str, limit: int) -> str | None:
    value = " ".join((value or "").split())
    if not value:
        return None
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def date10(value: str | None) -> str | None:
    value = (value or "").strip()
    return value[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", value) else None


def dmy(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    day, month, year = text(element, "DAY"), text(element, "MONTH"), text(element, "YEAR")
    if not (day and month and year):
        return None
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


# ----------------------------------------------------------------------------------------------- eForms

def parse_eforms(root: ET.Element, number: str) -> list[dict[str, Any]]:
    orgs: dict[str, dict[str, Any]] = {}
    for org in root.iter("Organization"):
        company = org.find("Company")
        if company is None:
            continue
        oid = text(company, "PartyIdentification/ID")
        orgs[oid] = {
            "name": text(company, "PartyName/Name"),
            "identifier": text(company, "PartyLegalEntity/CompanyID") or None,
            "country": alpha3(text(company, "PostalAddress/Country/IdentificationCode")),
            "size": text(company, "CompanySizeCode") or None,
        }

    lots: dict[str, dict[str, Any]] = {}
    n_lots = 0
    for lot in root.findall("ProcurementProjectLot"):
        lot_id_el = lot.find("ID")
        if lot_id_el is None or lot_id_el.get("schemeName", "Lot") != "Lot":
            continue
        n_lots += 1
        project = lot.find("ProcurementProject")
        cpv = [text(project, "MainCommodityClassification/ItemClassificationCode")]
        cpv += [(c.text or "").strip() for c in project.findall("AdditionalCommodityClassification/ItemClassificationCode")] \
            if project is not None else []
        quantity = project.find("EstimatedOverallContractQuantity") if project is not None else None
        framework_codes = [
            (c.text or "").strip() for c in lot.iter("ContractingSystemTypeCode")
            if c.get("listName") == "framework-agreement"
        ]
        est = amount(project, "RequestedTenderTotal/EstimatedOverallContractAmount")
        fw_max = amount(project, "RequestedTenderTotal/EstimatedOverallFrameworkContractsAmount")
        lots[(lot_id_el.text or "").strip()] = {
            "title": clip(text(project, "Name"), TITLE_CHARS),
            "description": clip(text(project, "Description"), DESCRIPTION_CHARS),
            "cpv_codes": list(dict.fromkeys(c for c in cpv if c)),
            "quantity": num(quantity.text) if quantity is not None else None,
            "quantity_unit": (quantity.get("unitCode") or None) if quantity is not None else None,
            "estimated_value": est["value"] if est else None,
            "estimated_currency": est["currency"] if est else None,
            "framework": any(c.startswith("fa-") for c in framework_codes),
            "framework_max": fw_max["value"] if fw_max else None,
            "framework_currency": fw_max["currency"] if fw_max else None,
        }

    result = root.find(".//NoticeResult")
    if result is None:
        return []
    notice_total = amount(result, "TotalAmount")
    parties = {text(p, "ID"): [text(t, "ID") for t in p.findall("Tenderer")] for p in result.findall("TenderingParty")}
    group_parties = {text(p, "ID") for p in result.findall("TenderingParty") if len(p.findall("Tenderer")) > 1}
    tenders = {text(t, "ID"): t for t in result.findall("LotTender")}
    contracted: dict[str, ET.Element] = {}
    for contract in result.findall("SettledContract"):
        for ref in contract.findall("LotTender"):
            contracted[text(ref, "ID")] = contract
    lot_results = result.findall("LotResult")
    awarded_results = [r for r in lot_results if text(r, "TenderResultCode") in ("selec-w", "")]

    records: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for lot_result in lot_results:
        lot_id = text(lot_result, "TenderLot/ID")
        code = text(lot_result, "TenderResultCode") or None
        refs = [text(t, "ID") for t in lot_result.findall("LotTender")]
        listed = [tenders[r] for r in refs if r in tenders]
        if contracted:
            winning = [t for t in listed if text(t, "ID") in contracted]
        else:
            winning = listed
        confirmed = bool(contracted) or len(listed) <= 1
        awarded = code in ("selec-w", None) and bool(winning)

        winners: list[dict[str, Any]] = []
        values: list[dict[str, Any]] = []
        award_dates, contract_dates = [], []
        for tender in winning if awarded else []:
            party = text(tender, "TenderingParty/ID")
            value = amount(tender, "LegalMonetaryTotal/PayableAmount")
            if value:
                values.append(value)
            contract = contracted.get(text(tender, "ID"))
            if contract is not None:
                award_dates.append(date10(text(contract, "AwardDate")))
                contract_dates.append(date10(text(contract, "IssueDate")))
            for member in parties.get(party, []):
                org = orgs.get(member, {})
                if not org.get("name"):
                    continue
                winners.append({**org, "tender": text(tender, "ID"), "party": party,
                                "group": party in group_parties,
                                "tender_value": value["value"] if value else None,
                                "tender_currency": value["currency"] if value else None})

        lot_value = None
        currency = None
        value_source = None
        currencies = {v["currency"] for v in values}
        if awarded and values and len(currencies) == 1 and len(values) == len(winning):
            lot_value = round(sum(v["value"] for v in values), 2)
            currency = values[0]["currency"]
            value_source = "tender_value"           # BT-720 of the lot's winning tender(s)
        elif awarded and not values and notice_total and len(awarded_results) == 1 and len(winning) == 1:
            lot_value = notice_total["value"]
            currency = notice_total["currency"]
            value_source = "notice_total_one_lot"   # BT-161, the notice awards only this lot
        bids = None
        for stat in lot_result.findall("ReceivedSubmissionsStatistics"):
            if text(stat, "StatisticsCode") == "tenders" and text(stat, "StatisticsNumeric").isdigit():
                bids = int(text(stat, "StatisticsNumeric"))
        fw_max = amount(lot_result, "FrameworkAgreementValues/MaximumValueAmount")
        key = lot_id or text(lot_result, "ID") or f"RES-{len(records)}"
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            key = f"{key}/{text(lot_result, 'ID') or seen[key]}"
        base = lots.get(lot_id, {})
        records.append({
            "publication_number": number,
            "format": "eforms",
            "lot_key": key,
            "lot_id": lot_id or None,
            "n_lots_in_notice": n_lots,
            **{k: base.get(k) for k in ("title", "description", "cpv_codes", "quantity", "quantity_unit",
                                       "estimated_value", "estimated_currency", "framework")},
            "framework_max": (fw_max["value"] if fw_max else None) or base.get("framework_max"),
            "framework_currency": (fw_max["currency"] if fw_max else None) or base.get("framework_currency"),
            "result_code": code,
            "awarded": awarded,
            "winner_confirmed": confirmed,
            "n_winning_tenders": len(winning) if awarded else 0,
            "winners": winners,
            "value": lot_value,
            "currency": currency,
            "value_source": value_source,
            "bids_received": bids,
            "award_date": next((d for d in award_dates if d), None),
            "contract_date": next((d for d in contract_dates if d), None),
        })
    return records


# ----------------------------------------------------------------------------------------------- legacy

def _legacy_form(root: ET.Element) -> ET.Element | None:
    section = root.find("FORM_SECTION")
    if section is None:
        return None
    forms = list(section)
    for form in forms:
        if form.get("CATEGORY") == "ORIGINAL":
            return form
    return forms[0] if forms else None


def _legacy_contractor(element: ET.Element) -> dict[str, Any] | None:
    org = element.find(".//OFFICIALNAME/..")
    if org is None:
        return None
    name = " ".join((text(org, "OFFICIALNAME")).split())
    if not name:
        return None
    country = element.find(".//COUNTRY")
    sme = None
    if element.find("SME") is not None:
        sme = True
    elif element.find("NO_SME") is not None:
        sme = False
    return {"name": name, "identifier": text(org, "NATIONALID") or None,
            "country": alpha3(country.get("VALUE")) if country is not None else None, "sme": sme}


def parse_legacy(root: ET.Element, number: str) -> list[dict[str, Any]]:
    form = _legacy_form(root)
    if form is None:
        return []
    tag = form.tag
    notice_cpv = [c.get("CODE") for c in root.iter("ORIGINAL_CPV") if c.get("CODE")]
    framework = any("FRAMEWORK" in (e.tag or "") or "FRAMEWORK" in " ".join(e.attrib.values()) for e in form.iter())

    # lot descriptions (2014 forms: OBJECT_DESCR with LOT_NO)
    lot_info: dict[str, dict[str, Any]] = {}
    for descr in form.iter("OBJECT_DESCR"):
        lot_no = text(descr, "LOT_NO") or descr.get("ITEM", "")
        cpv = [c.get("CODE") for c in descr.iter("CPV_CODE") if c.get("CODE")]
        est = descr.find("VAL_OBJECT")
        lot_info[lot_no] = {
            "title": clip(paragraphs(descr.find("TITLE")), TITLE_CHARS),
            "description": clip(paragraphs(descr.find("SHORT_DESCR")), DESCRIPTION_CHARS),
            "cpv_codes": cpv,
            "estimated_value": num(est.text) if est is not None else None,
            "estimated_currency": est.get("CURRENCY") if est is not None else None,
        }
    n_lots = max(len(lot_info), 1)
    notice_title = clip(paragraphs(form.find(".//TITLE_CONTRACT")) or paragraphs(form.find("OBJECT_CONTRACT/TITLE")), TITLE_CHARS)
    notice_descr = clip(paragraphs(form.find(".//SHORT_CONTRACT_DESCRIPTION")) or paragraphs(form.find("OBJECT_CONTRACT/SHORT_DESCR")),
                        DESCRIPTION_CHARS)

    records: list[dict[str, Any]] = []
    if form.find(".//AWARD_CONTRACT") is not None:
        fmt = "legacy_2014"
        items = form.findall("AWARD_CONTRACT")
    else:
        fmt = "legacy_r208"
        items = [e for e in form.iter() if local(e.tag).startswith("AWARD_OF_CONTRACT")]
    for index, item in enumerate(items, start=1):
        if fmt == "legacy_2014":
            lot_no = text(item, "LOT_NO")
            title = clip(paragraphs(item.find("TITLE")), TITLE_CHARS)
            awarded_el = item.find("AWARDED_CONTRACT")
            not_awarded = item.find("NO_AWARDED_CONTRACT")
            awarded = awarded_el is not None
            if awarded:
                result_code = "awarded"
            elif not_awarded is not None and not_awarded.find(".//PROCUREMENT_DISCONTINUED") is not None:
                result_code = "discontinued"
            elif not_awarded is not None:
                result_code = "unsuccessful"
            else:
                result_code = None
            scope = awarded_el if awarded_el is not None else item
            contractors = [c for c in (_legacy_contractor(x) for x in scope.iter("CONTRACTOR")) if c]
            group = scope.find(".//AWARDED_TO_GROUP") is not None
            bids = text(scope, ".//NB_TENDERS_RECEIVED")
            value_el = scope.find(".//VAL_TOTAL")
            est_el = scope.find(".//VAL_ESTIMATED_TOTAL")
            award_date = date10(text(scope, "DATE_CONCLUSION_CONTRACT"))
        else:
            lot_no = text(item, "LOT_NUMBER")
            title = clip(paragraphs(item.find("CONTRACT_TITLE")), TITLE_CHARS)
            contractors = [c for c in (_legacy_contractor(x) for x in item.iter("ECONOMIC_OPERATOR_NAME_ADDRESS")) if c]
            awarded = bool(contractors)
            result_code = "awarded" if awarded else None
            group = len(contractors) > 1
            bids = text(item, "OFFERS_RECEIVED_NUMBER")
            info = item.find("CONTRACT_VALUE_INFORMATION")
            value_el = None
            est_el = None
            if info is not None:
                cost = info.find("COSTS_RANGE_AND_CURRENCY_WITH_VAT_RATE")
                if cost is not None and cost.find("VALUE_COST") is not None:
                    value_el = cost.find("VALUE_COST")
                    value_el.set("CURRENCY", cost.get("CURRENCY", ""))
                est_parent = info.find("INITIAL_ESTIMATED_TOTAL_VALUE_CONTRACT")
                if est_parent is not None and est_parent.find(".//VALUE_COST") is not None:
                    est_el = est_parent.find(".//VALUE_COST")
                    est_el.set("CURRENCY", est_parent.get("CURRENCY", "")
                               or (est_parent.find(".//*[@CURRENCY]").get("CURRENCY") if est_parent.find(".//*[@CURRENCY]") is not None else ""))
            award_date = dmy(item.find("CONTRACT_AWARD_DATE"))
        info = lot_info.get(lot_no, {}) if lot_no else (next(iter(lot_info.values())) if len(lot_info) == 1 else {})
        value = None
        if value_el is not None:
            value = num(value_el.get("FMTVAL") or value_el.text)
        winners = [{**c, "group": group} for c in contractors]
        records.append({
            "publication_number": number,
            "format": fmt,
            "lot_key": f"item-{item.get('ITEM') or index}",
            "lot_id": lot_no or None,
            "n_lots_in_notice": n_lots,
            "title": title or info.get("title") or (notice_title if n_lots == 1 else None),
            "description": info.get("description") or (notice_descr if n_lots == 1 else None),
            "cpv_codes": list(dict.fromkeys((info.get("cpv_codes") or []) + notice_cpv)),
            "quantity": None,                      # legacy forms have no structured quantity (free text only)
            "quantity_unit": None,
            "estimated_value": info.get("estimated_value") or (num(est_el.get("FMTVAL") or est_el.text) if est_el is not None else None),
            "estimated_currency": info.get("estimated_currency") or (est_el.get("CURRENCY") if est_el is not None else None),
            "framework": framework,
            "framework_max": None,
            "framework_currency": None,
            "result_code": result_code,
            "awarded": awarded,
            "winner_confirmed": True,
            "n_winning_tenders": 1 if awarded and contractors else 0,
            "winners": winners if awarded else [],
            "value": value if awarded else None,
            "currency": (value_el.get("CURRENCY") or None) if (awarded and value_el is not None) else None,
            "value_source": "legacy_contract_value" if (awarded and value is not None) else None,
            "bids_received": int(bids) if bids.isdigit() else None,
            "award_date": award_date,
            "contract_date": award_date if fmt == "legacy_2014" else None,
        })
    return records


def parse_notice(path: Path, number: str) -> tuple[str, list[dict[str, Any]]]:
    try:
        root = strip_namespaces(parse_xml(path).getroot())
    except ET.ParseError:
        return "unparseable", []
    if root.tag == "TED_EXPORT":
        return "legacy", parse_legacy(root, number)
    if root.tag in ("ContractAwardNotice", "ContractNotice", "PriorInformationNotice"):
        return "eforms", parse_eforms(root, number)
    return f"unknown:{root.tag}", []


# ----------------------------------------------------------------------------------------------- push

def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def push(records: list[dict[str, Any]], snapshot: str, batch: int = 200) -> int:
    """Upsert records into kb_baseline_lots (service role; the snapshot must still be 'building')."""
    load_env(ROOT / ".env")
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise SystemExit("set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY (environment or .env) to --push")
    columns = ("publication_number", "lot_key", "format", "lot_id", "n_lots_in_notice", "title", "description",
               "cpv_codes", "quantity", "quantity_unit", "estimated_value", "estimated_currency", "framework",
               "framework_max", "framework_currency", "result_code", "awarded", "winner_confirmed",
               "n_winning_tenders", "winners", "value", "currency", "value_source", "bids_received",
               "award_date", "contract_date")
    sent = 0
    for start in range(0, len(records), batch):
        rows = [{"snapshot_id": snapshot, **{c: r.get(c) for c in columns}} for r in records[start:start + batch]]
        request = Request(
            f"{url.rstrip('/')}/rest/v1/kb_baseline_lots?on_conflict=snapshot_id,publication_number,lot_key",
            data=json.dumps(rows).encode(), method="POST",
            headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                     "Prefer": "resolution=merge-duplicates,return=minimal"})
        with urlopen(request, timeout=120, context=_SSL):
            sent += len(rows)
    return sent


# ----------------------------------------------------------------------------------------------- main

def read_list(path: Path) -> list[str]:
    with path.open() as handle:
        if path.suffix == ".csv":
            return [row["publication_number"].strip() for row in csv.DictReader(handle) if row.get("publication_number")]
        return [line.strip() for line in handle if line.strip() and not line.startswith("#")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--list", type=Path, required=True, help="CSV with publication_number (or one number per line)")
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    parser.add_argument("--out", type=Path, default=ROOT / ".cache" / "kb_baseline_lots.jsonl")
    parser.add_argument("--fetch-only", action="store_true", help="download into the cache, do not parse")
    parser.add_argument("--offline", action="store_true", help="parse cached notices only, never download")
    parser.add_argument("--push", action="store_true", help="upsert into kb_baseline_lots (service role)")
    parser.add_argument("--snapshot", default="baseline_v2")
    args = parser.parse_args()

    numbers = list(dict.fromkeys(read_list(args.list)))
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    stats = {"notices": len(numbers), "fetched_or_cached": 0, "missing": 0, "parsed": 0, "lots": 0}
    formats: dict[str, int] = {}
    for i, number in enumerate(numbers, start=1):
        path = args.cache_dir / f"{number}.xml"
        if not args.offline:
            path = fetch_xml(number, args.cache_dir) or path
        if not path.exists():
            stats["missing"] += 1
            continue
        stats["fetched_or_cached"] += 1
        if i % 25 == 0:
            print(f"  {i}/{len(numbers)}", file=sys.stderr)
        if args.fetch_only:
            continue
        kind, lots = parse_notice(path, number)
        formats[kind] = formats.get(kind, 0) + 1
        stats["parsed"] += 1 if lots else 0
        records.extend(lots)
    stats["lots"] = len(records)
    if not args.fetch_only:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        stats["formats"] = formats
        stats["out"] = str(args.out)
    if args.push:
        stats["pushed"] = push(records, args.snapshot)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
