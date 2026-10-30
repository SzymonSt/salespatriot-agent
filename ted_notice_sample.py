#!/usr/bin/env python3
"""Save full sample TED notices: every non-empty Search API field plus the eForms XML."""

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API_URL = "https://api.ted.europa.eu/v3/notices/search"
XML_URL = "https://ted.europa.eu/en/notice/{number}/xml"
USER_AGENT = "ted-notice-sample-script/1.0"
# Keep English and the multilingual link; the other 23 languages only repeat the URL.
LINK_LANGUAGES = ("ENG", "MUL")


def is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def trim_links(links: Any) -> Any:
    if not isinstance(links, dict):
        return links
    return {
        kind: {lang: url for lang, url in by_lang.items() if lang in LINK_LANGUAGES}
        for kind, by_lang in links.items()
        if isinstance(by_lang, dict)
    }


def fetch_notice(number: str, fields: list[str]) -> dict[str, Any] | None:
    body = json.dumps(
        {
            "query": f"publication-number={number}",
            "fields": fields,
            "limit": 1,
            "scope": "ALL",
        }
    ).encode("utf-8")
    request = Request(
        API_URL,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urlopen(request, timeout=60) as response:
        notices = json.load(response).get("notices") or []
    if not notices:
        return None
    notice = {key: value for key, value in notices[0].items() if not is_empty(value)}
    if "links" in notice:
        notice["links"] = trim_links(notice["links"])
    return dict(sorted(notice.items()))


def fetch_xml(number: str) -> bytes:
    request = Request(XML_URL.format(number=number), headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=60) as response:
        return response.read()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch TED notices with every Search API field that has a value, and their "
            "eForms XML, to show the exact data format."
        )
    )
    parser.add_argument("numbers", nargs="+", help="publication numbers, e.g. 749708-2026")
    parser.add_argument(
        "--fields-file",
        type=Path,
        default=Path("data/ted_search_fields.txt"),
        help="one Search API field name per line (default: data/ted_search_fields.txt)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("data/samples"),
        help="output directory (default: data/samples)",
    )
    parser.add_argument("--no-xml", action="store_true", help="skip the eForms XML")
    args = parser.parse_args()

    fields = [line.strip() for line in args.fields_file.read_text().splitlines() if line.strip()]
    args.out_dir.mkdir(parents=True, exist_ok=True)

    for number in args.numbers:
        try:
            notice = fetch_notice(number, fields)
            xml = None if args.no_xml else fetch_xml(number)
        except HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")
            print(f"{number}: HTTP {error.code}: {details}", file=sys.stderr)
            return 1
        except URLError as error:
            print(f"{number}: could not reach TED: {error.reason}", file=sys.stderr)
            return 1
        if notice is None:
            print(f"{number}: not found", file=sys.stderr)
            continue
        json_path = args.out_dir / f"{number}.json"
        json_path.write_text(json.dumps(notice, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{number}: {len(notice)} non-empty fields -> {json_path}")
        if xml is not None:
            xml_path = args.out_dir / f"{number}.xml"
            xml_path.write_bytes(xml)
            print(f"{number}: eForms XML ({len(xml) // 1024} KB) -> {xml_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
