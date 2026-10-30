#!/usr/bin/env python3
"""Build buyer (demand) and supplier (supply) profiles from TED weapons and ammunition notices."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://api.ted.europa.eu/v3/notices/search"
PAGE_SIZE = 250
MAX_RETRIES = 5
RETRY_DELAY_SECONDS = 5
REQUEST_INTERVAL_SECONDS = 1
COMPETITION_TYPES = (
    "qu-sy pin-cfc-standard subco cn-desg cn-social cn-standard pin-cfc-social"
)
AWARD_TYPES = "can-standard can-desg can-social can-tran"
# CPV 35300000 "Weapons, ammunition and associated parts" and its subtree.
SCOPE = "classification-cpv IN (35300000) AND contract-nature IN (supplies)"
FOCUS_CATEGORIES = {
    "3533": "Ammunition",
    "3532": "Firearms",
    "3534": "Parts of firearms and ammunition",
    "3531": "Miscellaneous weapons",
}
DISCOVERY_QUERIES = {
    "buyer": f"notice-type IN ({COMPETITION_TYPES}) AND {SCOPE}",
    "supplier": f"notice-type IN ({AWARD_TYPES}) AND {SCOPE}",
}
NOTICE_FIELDS = [
    "publication-number",
    "publication-date",
    "notice-type",
    "notice-title",
    "title-proc",
    "description-proc",
    "classification-cpv",
    "buyer-name",
    "buyer-identifier",
    "buyer-country",
    "buyer-city",
    "buyer-internet-address",
    "buyer-legal-type",
    "main-activity",
    "winner-name",
    "winner-identifier",
    "winner-country",
    "winner-city",
    "winner-internet-address",
    "winner-size",
    "estimated-value-proc",
    "estimated-value-cur-proc",
    "result-value-notice",
    "result-value-cur-notice",
]
# Per role: the organisation's own fields, then the counterpart's name field.
ROLE_FIELDS = {
    "buyer": {
        "name": "buyer-name",
        "identifier": "buyer-identifier",
        "country": "buyer-country",
        "city": "buyer-city",
        "website": "buyer-internet-address",
        "counterpart": "winner-name",
    },
    "supplier": {
        "name": "winner-name",
        "identifier": "winner-identifier",
        "country": "winner-country",
        "city": "winner-city",
        "website": "winner-internet-address",
        "counterpart": "buyer-name",
    },
}


def search(query: str, limit: int, page: int = 1) -> dict[str, Any]:
    body = json.dumps(
        {"query": query, "fields": NOTICE_FIELDS, "limit": limit, "page": page}
    ).encode("utf-8")
    request = Request(
        API_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ted-company-profiles-script/1.0",
        },
        method="POST",
    )
    for attempt in range(MAX_RETRIES):
        try:
            with urlopen(request, timeout=60) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code != 429 or attempt == MAX_RETRIES - 1:
                raise
            time.sleep(RETRY_DELAY_SECONDS * 2**attempt)
    raise AssertionError("unreachable")


def fetch(query: str, count: int) -> tuple[list[dict[str, Any]], int]:
    notices: list[dict[str, Any]] = []
    total = 0
    page = 1
    # Pages are offset by limit, so keep it fixed and trim the last page.
    limit = min(PAGE_SIZE, count)
    while len(notices) < count:
        time.sleep(REQUEST_INTERVAL_SECONDS)
        result = search(f"{query} SORT BY publication-number DESC", limit, page)
        batch = result.get("notices") or []
        total = result.get("totalNoticeCount", total)
        notices.extend(batch)
        if len(batch) < limit:
            break
        page += 1
    return notices[:count], total


def texts(value: Any) -> list[str]:
    """Flatten a TED value (str, list or {language: str | list}) to unique strings."""
    if isinstance(value, dict):
        preferred = value.get("eng") or next(iter(value.values()), [])
        return texts(preferred)
    if isinstance(value, list):
        return list(dict.fromkeys(str(item) for item in value if item))
    return [str(value)] if value else []


def first(value: Any) -> str:
    items = texts(value)
    return items[0] if items else ""


def load_cpv(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8") as cpv_file:
        return {row["code"]: row["label"] for row in csv.DictReader(cpv_file)}


def organisation_key(notice: dict[str, Any], role: str) -> tuple[str, str] | None:
    """Return (identifier, name) when the notice names exactly one organisation."""
    fields = ROLE_FIELDS[role]
    names = texts(notice.get(fields["name"]))
    if len(names) != 1:
        return None
    return first(notice.get(fields["identifier"])), names[0]


def focus_categories(notice: dict[str, Any]) -> set[str]:
    codes = texts(notice.get("classification-cpv"))
    return {
        label
        for prefix, label in FOCUS_CATEGORIES.items()
        if any(code.startswith(prefix) for code in codes)
    }


def in_focus(notice: dict[str, Any]) -> bool:
    return any(code.startswith("353") for code in texts(notice.get("classification-cpv")))


def discover(role: str, scan: int, count: int) -> list[dict[str, Any]]:
    notices, _ = fetch(DISCOVERY_QUERIES[role], scan)
    scores: Counter[tuple[str, str]] = Counter()
    samples: dict[tuple[str, str], dict[str, Any]] = {}
    for notice in notices:
        key = organisation_key(notice, role)
        if key is None or not in_focus(notice):
            continue
        scores[key] += 1
        samples.setdefault(key, notice)

    # Round-robin across countries: every country's best organisation first,
    # then every country's second best, and so on.
    country_rank: Counter[str] = Counter()
    ranked = []
    for key, score in scores.most_common():
        country = first(samples[key].get(ROLE_FIELDS[role]["country"]))
        ranked.append((country_rank[country], -score, key))
        country_rank[country] += 1
    return [
        {"identifier": key[0], "name": key[1], "sample": samples[key]}
        for _, _, key in sorted(ranked)[:count]
    ]


def history_query(role: str, identifier: str, name: str) -> str:
    fields = ROLE_FIELDS[role]
    if identifier:
        return f'{fields["identifier"]}="{identifier}"'
    return f'{fields["name"]}="{name}"'


def notice_value(notice: dict[str, Any]) -> tuple[str, str]:
    if notice.get("result-value-notice"):
        return first(notice["result-value-notice"]), first(
            notice.get("result-value-cur-notice")
        )
    return first(notice.get("estimated-value-proc")), first(
        notice.get("estimated-value-cur-proc")
    )


def notice_summary(notice: dict[str, Any], role: str) -> dict[str, Any]:
    number = first(notice.get("publication-number"))
    value, currency = notice_value(notice)
    description = first(notice.get("description-proc"))
    return {
        "notice_number": number,
        "published": first(notice.get("publication-date"))[:10],
        "notice_type": first(notice.get("notice-type")),
        "headline": first(notice.get("notice-title")),
        "title": first(notice.get("title-proc")),
        "description": description[:600],
        "cpv_codes": texts(notice.get("classification-cpv")),
        "value": value,
        "currency": currency,
        "counterparts": texts(notice.get(ROLE_FIELDS[role]["counterpart"])),
        "ted_url": f"https://ted.europa.eu/en/notice/-/detail/{number}",
    }


def latest_versions(notices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop republished or corrected copies; notices arrive newest first."""
    seen: set[tuple[str, ...]] = set()
    unique = []
    for notice in notices:
        key = (
            first(notice.get("notice-type")),
            first(notice.get("title-proc")),
            *notice_value(notice),
        )
        if key not in seen:
            seen.add(key)
            unique.append(notice)
    return unique


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def build_profile(
    role: str,
    candidate: dict[str, Any],
    history_size: int,
    cpv_labels: dict[str, str],
) -> dict[str, Any]:
    fields = ROLE_FIELDS[role]
    identifier, name = candidate["identifier"], candidate["name"]
    notices, total = fetch(history_query(role, identifier, name), history_size)
    notices = latest_versions(notices)
    sample = candidate["sample"]
    country = first(sample.get(fields["country"]))

    cpv_counts: Counter[str] = Counter()
    counterparts: Counter[str] = Counter()
    awarded: Counter[str] = Counter()
    for notice in notices:
        cpv_counts.update(set(texts(notice.get("classification-cpv"))))
        counterparts.update(texts(notice.get(fields["counterpart"])))
        # Notice-level award values are only attributable to a sole winner.
        sole_winner = len(texts(notice.get("winner-name"))) == 1
        if role == "supplier" and sole_winner and notice.get("result-value-notice"):
            value, currency = notice_value(notice)
            try:
                amount = float(value)
            except ValueError:
                continue
            # Undisclosed values are published as 0.
            if amount > 0:
                awarded[currency or "EUR"] += amount
    counterparts.pop(name, None)

    dates = sorted(first(n.get("publication-date"))[:10] for n in notices)
    profile: dict[str, Any] = {
        "id": f"{role}-{country.lower()}-{slug(identifier or name)}",
        "role": "demand" if role == "buyer" else "supply",
        "name": name,
        "country": country,
        "city": first(sample.get(fields["city"])),
        "website": first(sample.get(fields["website"])),
        "identifier": identifier,
        "summary": "",
    }
    if role == "buyer":
        profile["legal_type"] = first(sample.get("buyer-legal-type"))
        profile["main_activity"] = first(sample.get("main-activity"))
    else:
        profile["company_size"] = first(sample.get("winner-size"))
    profile["ted_activity"] = {
        "notices_total": total,
        "notices_analysed": len(notices),
        "weapons_ammo_notices_analysed": sum(in_focus(n) for n in notices),
        "first_seen": dates[0] if dates else "",
        "last_seen": dates[-1] if dates else "",
    }
    if role == "supplier":
        profile["ted_activity"]["sole_winner_awarded_value"] = {
            currency: round(amount, 2) for currency, amount in awarded.items()
        }
    profile["top_cpv"] = [
        {"code": code, "label": cpv_labels.get(code, ""), "notices": count}
        for code, count in cpv_counts.most_common(10)
    ]
    profile["top_counterparts"] = [
        {"name": counterpart, "notices": count}
        for counterpart, count in counterparts.most_common(10)
    ]
    category_counts: Counter[str] = Counter()
    for notice in notices:
        category_counts.update(focus_categories(notice))
    profile["weapons_ammo_categories"] = {
        label: category_counts[label] for label in FOCUS_CATEGORIES.values()
    }
    profile["recent_notices"] = [
        notice_summary(n, role) for n in notices if in_focus(n)
    ][:10]
    return profile


def existing_profiles(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    profiles = json.loads(path.read_text(encoding="utf-8"))
    return {p["id"]: p for p in profiles}


def carry_over(profile: dict[str, Any], previous: dict[str, Any]) -> None:
    """Keep the hand-written summary and fields added by other enrichment scripts."""
    profile["summary"] = previous.get("summary", "")
    for key, value in previous.items():
        profile.setdefault(key, value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Pick buyers and suppliers active in TED weapons and ammunition "
            "supply notices and build a profile for each from their notice history."
        )
    )
    parser.add_argument(
        "--buyers",
        type=int,
        default=5,
        help="buyer profiles to build; 0 leaves buyers.json untouched (default: 5)",
    )
    parser.add_argument(
        "--suppliers",
        type=int,
        default=20,
        help=(
            "supplier profiles to build; 0 leaves suppliers.json untouched "
            "(default: 20)"
        ),
    )
    parser.add_argument(
        "--scan",
        type=int,
        default=1000,
        help="recent notices scanned per role to find candidates (default: 1000)",
    )
    parser.add_argument(
        "--history",
        type=int,
        default=100,
        help="notices fetched per organisation for its profile (default: 100)",
    )
    parser.add_argument(
        "--kb-dir",
        type=Path,
        default=Path("knowledge_base"),
        help="knowledge base directory (default: knowledge_base)",
    )
    args = parser.parse_args()
    if min(args.buyers, args.suppliers) < 0:
        parser.error("--buyers and --suppliers must not be negative")
    if min(args.scan, args.history) < 1:
        parser.error("--scan and --history must be positive integers")

    cpv_labels = load_cpv(args.kb_dir / "cpv_codes.csv")
    out_dir = args.kb_dir / "profiles"
    out_dir.mkdir(parents=True, exist_ok=True)

    roles = (
        ("buyer", "buyers.json", args.buyers),
        ("supplier", "suppliers.json", args.suppliers),
    )
    for role, filename, count in roles:
        if count == 0:
            continue
        path = out_dir / filename
        previous = existing_profiles(path)
        try:
            candidates = discover(role, args.scan, count)
            profiles = [
                build_profile(role, c, args.history, cpv_labels)
                for c in candidates
            ]
        except HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            print(f"TED API returned HTTP {error.code}: {details}", file=sys.stderr)
            return 1
        except URLError as error:
            print(f"Could not reach the TED API: {error.reason}", file=sys.stderr)
            return 1

        for profile in profiles:
            carry_over(profile, previous.get(profile["id"], {}))
        path.write_text(
            json.dumps(profiles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Saved {len(profiles)} {role} profiles to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
