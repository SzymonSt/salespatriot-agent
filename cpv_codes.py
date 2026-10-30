#!/usr/bin/env python3
"""Download the CPV 2008 vocabulary and extract the defence-related codes."""

import argparse
import csv
import io
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SPARQL_URL = "https://publications.europa.eu/webapi/rdf/sparql"
CPV_SCHEME = "http://data.europa.eu/cpv/cpv"
QUERY = f"""
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?code ?label ?parent WHERE {{
  ?concept skos:inScheme <{CPV_SCHEME}> ;
           skos:notation ?code ;
           skos:prefLabel ?label .
  FILTER(lang(?label) = "{{language}}")
  OPTIONAL {{ ?concept skos:broader ?broader . ?broader skos:notation ?parent }}
}}
"""

# Each root pulls in its whole subtree. Codes are grouped so callers can
# filter, e.g. to equipment only when matching tenders to products.
DEFENCE_ROOTS = {
    "equipment": {
        "35300000": "Weapons, ammunition and associated parts",
        "35400000": "Military vehicles and associated parts",
        "35500000": "Warships and associated parts",
        "35600000": "Military aircrafts, missiles and spacecrafts",
        "35700000": "Military electronic systems",
        "35800000": "Individual and support equipment",
    },
    "materials": {
        "24600000": "Explosives",
        "44430000": "Armour plating",
        "39522200": "Camouflage covers",
    },
    "services": {
        "50600000": "Repair and maintenance of security and defence materials",
        "50840000": "Repair and maintenance of weapons and weapon systems",
        "51550000": "Installation services of weapon systems",
        "72231000": "Development of software for military applications",
        "73400000": "R&D on security and defence materials",
        "75211300": "Foreign military-aid-related services",
        "75220000": "Defence services",
        "80600000": "Training services in defence and security materials",
        "90523100": "Weapons and ammunition disposal services",
        "90523200": "Bomb-disposal services",
    },
    "works": {
        "45111310": "Dismantling works for military installations",
        "45216200": "Construction work for military buildings and installations",
        "45222200": "Engineering work for military installations",
    },
}


def significant_digits(code: str) -> str:
    """Return the code prefix that identifies its subtree (trailing zeros cut)."""
    stripped = code.rstrip("0")
    return stripped if len(stripped) >= 2 else code[:2]


def fetch_cpv(language: str) -> list[dict[str, str]]:
    query = QUERY.replace("{language}", language)
    request = Request(
        f"{SPARQL_URL}?{urlencode({'query': query})}",
        headers={"Accept": "text/csv", "User-Agent": "cpv-codes-script/1.0"},
    )
    with urlopen(request, timeout=60) as response:
        text = response.read().decode("utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def build_rows(records: list[dict[str, str]]) -> list[dict[str, str]]:
    rows = []
    for record in records:
        code = record["code"]
        rows.append(
            {
                "code": code,
                "label": record["label"],
                "parent_code": record.get("parent") or "",
                "division": code[:2],
                "level": str(len(significant_digits(code)) - 1),
            }
        )
    rows.sort(key=lambda row: row["code"])
    return rows


def defence_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    roots = [
        (significant_digits(root), category, root)
        for category, codes in DEFENCE_ROOTS.items()
        for root in codes
    ]
    selected = []
    for row in rows:
        for prefix, category, root in roots:
            if row["code"].startswith(prefix):
                selected.append({**row, "category": category, "root_code": root})
                break
    return selected


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Download CPV 2008 codes and the defence-related subset."
    )
    parser.add_argument(
        "--language",
        default="en",
        help="label language as an ISO 639-1 code (default: en)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("knowledge_base"),
        help="output directory (default: knowledge_base)",
    )
    args = parser.parse_args()

    try:
        records = fetch_cpv(args.language)
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace")
        print(f"SPARQL endpoint returned HTTP {error.code}: {details}", file=sys.stderr)
        return 1
    except URLError as error:
        print(f"Could not reach the SPARQL endpoint: {error.reason}", file=sys.stderr)
        return 1

    if not records:
        print(f"No CPV codes found for language '{args.language}'.", file=sys.stderr)
        return 1

    rows = build_rows(records)
    defence = defence_rows(rows)
    all_path = args.out_dir / "cpv_codes.csv"
    defence_path = args.out_dir / "cpv_defence_codes.csv"
    write_csv(all_path, rows)
    write_csv(defence_path, defence)
    print(f"Saved {len(rows)} CPV codes to {all_path}")
    print(f"Saved {len(defence)} defence-related codes to {defence_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
