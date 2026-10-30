#!/usr/bin/env python3
"""Add each supplier's won TED contracts (lot by lot) and a track record summary."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ted_company_profiles import fetch, first, load_cpv, texts


XML_URL = "https://ted.europa.eu/en/notice/{number}/xml"
AWARD_TYPES = "can-standard can-desg can-social can-tran can-modif veat"
MAX_RETRIES = 5
RETRY_DELAY_SECONDS = 5
DESCRIPTION_CHARS = 800
# Lot titles shorter than this ("Lot 1", "Generell del") say nothing useful.
GENERIC_TITLE_CHARS = 15


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def strip_namespaces(root: ET.Element) -> ET.Element:
    for element in root.iter():
        element.tag = local(element.tag)
    return root


def text(element: ET.Element | None, path: str) -> str:
    if element is None:
        return ""
    found = element.find(path)
    return (found.text or "").strip() if found is not None else ""


def amount(element: ET.Element | None, path: str) -> dict[str, Any] | None:
    if element is None:
        return None
    found = element.find(path)
    if found is None or not (found.text or "").strip():
        return None
    return {"value": float(found.text), "currency": found.get("currencyID", "")}


def normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def download_xml(number: str, cache_dir: Path) -> ET.Element | None:
    path = cache_dir / f"{number}.xml"
    if not path.exists():
        request = Request(
            XML_URL.format(number=number),
            headers={"User-Agent": "ted-supplier-contracts-script/1.0"},
        )
        for attempt in range(MAX_RETRIES):
            try:
                time.sleep(1)
                with urlopen(request, timeout=60) as response:
                    path.write_bytes(response.read())
                break
            except HTTPError as error:
                if error.code != 429 or attempt == MAX_RETRIES - 1:
                    raise
                time.sleep(RETRY_DELAY_SECONDS * 2**attempt)
    try:
        return strip_namespaces(ET.parse(path).getroot())
    except ET.ParseError:
        return None


def is_supplier(org: ET.Element, identifier: str, name: str) -> bool:
    company = org.find("Company")
    if company is None:
        return False
    company_id = normalise(text(company, "PartyLegalEntity/CompanyID"))
    wanted_id = normalise(identifier)
    if company_id and wanted_id and (company_id in wanted_id or wanted_id in company_id):
        return True
    return normalise(text(company, "PartyName/Name")) == normalise(name)


def lot_details(root: ET.Element) -> dict[str, dict[str, Any]]:
    lots = {}
    for lot in root.iter("ProcurementProjectLot"):
        project = lot.find("ProcurementProject")
        cpv = [text(project, "MainCommodityClassification/ItemClassificationCode")]
        cpv += [
            (code.text or "").strip()
            for code in project.iter("ItemClassificationCode")
        ] if project is not None else []
        duration = project.find("PlannedPeriod/DurationMeasure") if project is not None else None
        quantity = project.find(".//Quantity") if project is not None else None
        framework = any(
            (code.text or "").startswith("fa-")
            for code in lot.iter("ContractingSystemTypeCode")
        )
        lots[text(lot, "ID")] = {
            "title": text(project, "Name"),
            "description": text(project, "Description")[:DESCRIPTION_CHARS],
            "cpv_codes": list(dict.fromkeys(code for code in cpv if code)),
            "quantity": (
                f"{quantity.text.strip()} {quantity.get('unitCode', '')}".strip()
                if quantity is not None and quantity.text
                else ""
            ),
            "duration": (
                f"{duration.text.strip()} {duration.get('unitCode', '').lower()}"
                if duration is not None and duration.text
                else ""
            ),
            "framework_agreement": framework,
        }
    return lots


def won_lots(
    root: ET.Element, identifier: str, name: str
) -> list[dict[str, Any]]:
    """Return one record per lot result in which the supplier is a winner."""
    result = root.find(".//NoticeResult")
    if result is None:
        return []

    organisations = {
        text(org, "Company/PartyIdentification/ID"): org
        for org in root.iter("Organization")
    }
    party_members = {
        text(party, "ID"): [text(t, "ID") for t in party.findall("Tenderer")]
        for party in result.findall("TenderingParty")
    }
    tenders = {text(t, "ID"): t for t in result.findall("LotTender")}
    contracts = {}
    for contract in result.findall("SettledContract"):
        for tender in contract.findall("LotTender"):
            contracts[text(tender, "ID")] = contract
    lots = lot_details(root)

    records = []
    # A lot result lists every tender received; only tenders that ended in a
    # settled contract won. Without any contracts we cannot tell, so fall back
    # to all listed tenders and mark the winner as unconfirmed.
    confirmed = bool(contracts)
    for lot_result in result.findall("LotResult"):
        # Direct awards (VEAT) carry no result code.
        if text(lot_result, "TenderResultCode") not in ("selec-w", ""):
            continue
        co_winners: set[str] = set()
        ours = None
        for tender_ref in lot_result.findall("LotTender"):
            tender = tenders.get(text(tender_ref, "ID"))
            if tender is None or (confirmed and text(tender, "ID") not in contracts):
                continue
            for member in party_members.get(text(tender, "TenderingParty/ID"), []):
                org = organisations.get(member)
                if org is None:
                    continue
                if is_supplier(org, identifier, name):
                    ours = ours if ours is not None else tender
                else:
                    co_winners.add(text(org, "Company/PartyName/Name"))
        if ours is None:
            continue

        lot_id = text(lot_result, "TenderLot/ID")
        submissions = {
            text(stat, "StatisticsCode"): text(stat, "StatisticsNumeric")
            for stat in lot_result.findall("ReceivedSubmissionsStatistics")
        }
        contract = contracts.get(text(ours, "ID"))
        records.append(
            {
                "lot_id": lot_id,
                **lots.get(lot_id, {}),
                "awarded_value": amount(ours, "LegalMonetaryTotal/PayableAmount"),
                "tenders_received": int(submissions["tenders"])
                if submissions.get("tenders", "").isdigit()
                else None,
                "co_winners": sorted(co_winners - {""}),
                "subcontracting": text(ours, "SubcontractingTerm/TermCode"),
                "winner_confirmed": confirmed,
                "award_date": text(contract, "AwardDate")[:10],
                "contract_signed": text(contract, "IssueDate")[:10],
                "contract_title": text(contract, "Title"),
            }
        )
    return records


def award_notices(identifier: str, name: str, limit: int) -> list[dict[str, Any]]:
    # Identifiers are formatted inconsistently (5565005930 vs 556500-5930), so
    # also match on name; won_lots() then confirms the exact organisation.
    # Quotes inside the name would end the phrase and break the query.
    phrase = name.replace('"', " ")
    query = f'winner-name="{phrase}"'
    if identifier:
        query = f'(winner-identifier="{identifier}" OR {query})'
    notices, _ = fetch(f"notice-type IN ({AWARD_TYPES}) AND {query}", limit)
    return notices


def track_record(
    contracts: list[dict[str, Any]], cpv_labels: dict[str, str]
) -> dict[str, Any]:
    values: Counter[str] = Counter()
    shared_ceilings: Counter[str] = Counter()
    buyers: Counter[str] = Counter()
    buyer_countries: Counter[str] = Counter()
    products: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"lots": 0, "value": Counter(), "last_award": "", "examples": []}
    )
    competitive = sole_bid = frameworks = 0
    counted: set[tuple[str, str, float]] = set()
    for contract in contracts:
        value = contract.get("awarded_value")
        has_value = bool(value and value["value"] > 0)
        # Some buyers repeat one tender total on every lot; count it once.
        if has_value:
            key = (contract["notice_number"], value["currency"], value["value"])
            has_value = key not in counted
            counted.add(key)
        # With co-winners the value is usually a shared framework ceiling.
        shared = bool(contract.get("co_winners"))
        if has_value and shared:
            shared_ceilings[value["currency"]] += value["value"]
        elif has_value:
            values[value["currency"]] += value["value"]
        buyers[contract["buyer"]] += 1
        buyer_countries[contract["buyer_country"]] += 1
        tenders_received = contract.get("tenders_received")
        if tenders_received == 1:
            sole_bid += 1
        elif tenders_received:
            competitive += 1
        frameworks += bool(contract.get("framework_agreement"))

        main_cpv = (contract.get("cpv_codes") or ["unknown"])[0]
        product = products[main_cpv]
        product["lots"] += 1
        if has_value and not shared:
            product["value"][value["currency"]] += value["value"]
        product["last_award"] = max(product["last_award"], contract["published"])
        title = contract.get("title", "")
        if len(title) < GENERIC_TITLE_CHARS:
            title = contract.get("notice_title") or title
        if title and title not in product["examples"] and len(product["examples"]) < 5:
            product["examples"].append(title)

    dates = sorted(c["published"] for c in contracts)
    return {
        "lots_won": len(contracts),
        "notices_won": len({c["notice_number"] for c in contracts}),
        "first_award": dates[0] if dates else "",
        "last_award": dates[-1] if dates else "",
        "awarded_value": {k: round(v, 2) for k, v in values.items()},
        "shared_framework_ceilings": {
            k: round(v, 2) for k, v in shared_ceilings.items()
        },
        "distinct_buyers": len(buyers),
        "repeat_buyers": [
            {"name": buyer, "lots": count}
            for buyer, count in buyers.most_common()
            if count > 1
        ],
        "buyer_countries": dict(buyer_countries.most_common()),
        "competitive_wins": competitive,
        "sole_bid_wins": sole_bid,
        "framework_agreement_lots": frameworks,
        "proven_products": [
            {
                "cpv": code,
                "label": cpv_labels.get(code, ""),
                "lots": info["lots"],
                "awarded_value": {k: round(v, 2) for k, v in info["value"].items()},
                "last_award": info["last_award"],
                "examples": info["examples"],
            }
            for code, info in sorted(
                products.items(), key=lambda item: -item[1]["lots"]
            )
        ],
    }


def latest_contracts(contracts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop copies of the same award from corrected or republished notices.

    Matching lots inside one notice are kept: they are separate lots.
    """
    first_notice: dict[tuple[Any, ...], str] = {}
    unique = []
    for contract in contracts:
        value = contract.get("awarded_value") or {}
        key = (
            normalise(contract["buyer"]),
            normalise(contract.get("title", "") or contract["notice_title"]),
            value.get("value"),
            contract.get("contract_signed", ""),
        )
        notice = first_notice.setdefault(key, contract["notice_number"])
        if notice == contract["notice_number"]:
            unique.append(contract)
    return unique


def enrich(
    profile: dict[str, Any],
    limit: int,
    cache_dir: Path,
    cpv_labels: dict[str, str],
) -> None:
    identifier, name = profile["identifier"], profile["name"]
    contracts = []
    unparsed = []
    for notice in award_notices(identifier, name, limit):
        number = first(notice.get("publication-number"))
        root = download_xml(number, cache_dir)
        lots = won_lots(root, identifier, name) if root is not None else []
        if not lots:
            unparsed.append(number)
            continue
        for lot in lots:
            contracts.append(
                {
                    "notice_number": number,
                    "published": first(notice.get("publication-date"))[:10],
                    "notice_type": first(notice.get("notice-type")),
                    "notice_title": first(notice.get("title-proc")),
                    "buyer": first(notice.get("buyer-name")),
                    "buyer_country": first(notice.get("buyer-country")),
                    **lot,
                    "ted_url": f"https://ted.europa.eu/en/notice/-/detail/{number}",
                }
            )
    contracts.sort(key=lambda c: c["published"], reverse=True)
    contracts = latest_contracts(contracts)
    profile["track_record"] = track_record(contracts, cpv_labels)
    profile["track_record"]["notices_without_lot_detail"] = unparsed
    profile["contracts"] = contracts


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Add won TED contracts (lot level, from notice XML) and a track "
            "record summary to each supplier profile."
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="award notices fetched per supplier (default: 100)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data"),
        help="data directory (default: data)",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(".cache/ted_xml"),
        help="where notice XML files are cached (default: .cache/ted_xml)",
    )
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be a positive integer")

    path = args.data_dir / "profiles" / "suppliers.json"
    profiles = json.loads(path.read_text(encoding="utf-8"))
    cpv_labels = load_cpv(args.data_dir / "cpv_codes.csv")
    args.cache_dir.mkdir(parents=True, exist_ok=True)

    for profile in profiles:
        try:
            enrich(profile, args.limit, args.cache_dir, cpv_labels)
        except HTTPError as error:
            print(f"TED returned HTTP {error.code} for {profile['name']}", file=sys.stderr)
            return 1
        except URLError as error:
            print(f"Could not reach TED: {error.reason}", file=sys.stderr)
            return 1
        record = profile["track_record"]
        print(
            f"{profile['name']}: {record['lots_won']} lots in "
            f"{record['notices_won']} notices"
        )

    path.write_text(
        json.dumps(profiles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Updated {len(profiles)} supplier profiles in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
