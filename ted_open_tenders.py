#!/usr/bin/env python3
"""Export TED notices whose tender or participation deadline has not passed."""

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://api.ted.europa.eu/v3/notices/search"
DEFAULT_QUERY = "PD>=today(-30) AND (DT>=today(0) OR DD>=today(0))"
FIELDS = ["ND", "PD", "TI", "DT", "DD"]
CSV_COLUMNS = [
    "notice_number",
    "published",
    "title",
    "tender_deadline",
    "participation_deadline",
    "ted_url",
]


def display_value(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(dict.fromkeys(str(item) for item in value if item))
    if value is None:
        return ""
    return str(value)


def notice_title(value: Any) -> str:
    if not isinstance(value, dict):
        return display_value(value)
    return str(value.get("eng") or next(iter(value.values()), ""))


def notice_url(notice: dict[str, Any], number: str) -> str:
    links = notice.get("links")
    if isinstance(links, dict):
        for key in ("htmlDirect", "html"):
            language_links = links.get(key)
            if isinstance(language_links, dict) and language_links.get("ENG"):
                return str(language_links["ENG"])
    if number:
        return f"https://ted.europa.eu/en/notice/{number}/html"
    return ""


def fetch_notices(query: str, limit: int) -> dict[str, Any]:
    body = json.dumps(
        {"query": query, "fields": FIELDS, "limit": limit, "page": 1}
    ).encode("utf-8")
    request = Request(
        API_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "ted-open-tenders-script/1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Export TED notices published in the last 30 days with a future "
            "tender/participation deadline."
        )
    )
    parser.add_argument(
        "--query",
        default=DEFAULT_QUERY,
        help=(
            "TED expert query (default: published in the last 30 days, with "
            "DT or DD today or later)"
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="maximum notices to fetch from the first result page (default: 100)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("open_tenders.csv"),
        help="CSV output path (default: open_tenders.csv)",
    )
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be a positive integer")

    try:
        result = fetch_notices(args.query, args.limit)
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        print(f"TED API returned HTTP {error.code}: {details}", file=sys.stderr)
        return 1
    except URLError as error:
        print(f"Could not reach the TED API: {error.reason}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as error:
        print(f"The TED API returned invalid JSON: {error}", file=sys.stderr)
        return 1

    notices = result.get("notices")
    if not isinstance(notices, list):
        print("TED API response did not contain a notices list.", file=sys.stderr)
        return 1

    with args.out.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for notice in notices:
            if not isinstance(notice, dict):
                continue
            number = display_value(
                notice.get("publication-number") or notice.get("ND")
            )
            writer.writerow(
                {
                    "notice_number": number,
                    "published": display_value(notice.get("PD")),
                    "title": notice_title(notice.get("TI")),
                    "tender_deadline": display_value(notice.get("DT")),
                    "participation_deadline": display_value(notice.get("DD")),
                    "ted_url": notice_url(notice, number),
                }
            )

    total = result.get("totalNoticeCount", len(notices))
    print(f"Saved {len(notices)} notices (of {total} matches) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())