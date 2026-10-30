#!/usr/bin/env python3
"""Keep data/profiles/*.json in English using a translation memory.

TED notices and registers are in the buyer's language. Translations live in
data/translations.json ({original: English}), so they survive re-running the
fetch scripts: run this script afterwards to re-apply them.

  python3 translate_data.py --extract pending.json   # strings with no translation yet
  python3 translate_data.py                          # apply the memory in place

A profile's own `name` becomes its English display name; the registered name
is kept in `legal_name`, which the scripts use to match TED and registers.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator


PROFILE_FILES = ("profiles/buyers.json", "profiles/suppliers.json")
# Dotted paths into one profile; "[]" stands for a list item, "*" for any key.
# Only fields copied from TED or registers: text we wrote ourselves is English.
TRANSLATABLE = (
    "name",
    "city",
    "recent_notices[].headline",
    "recent_notices[].title",
    "recent_notices[].description",
    "recent_notices[].counterparts[]",
    "top_counterparts[].name",
    "track_record.repeat_buyers[].name",
    "track_record.proven_products[].examples[]",
    "contracts[].notice_title",
    "contracts[].title",
    "contracts[].description",
    "contracts[].contract_title",
    "contracts[].buyer",
    "contracts[].co_winners[]",
    "catalogue.items[].track_record.buyers[]",
    "due_diligence.categories.*.reasoning",
    "due_diligence.categories.*.findings[]",
    "due_diligence.categories.*.evidence[].fact",
    "due_diligence.categories.*.red_flags[]",
    "due_diligence.red_flags[]",
    "due_diligence.gates[].detail",
    # Registry wording (roles, share descriptions); names of people and owners stay as registered.
    "kyc.checks.*.summary",
    "kyc.checks.*.detail[].role",
    "kyc.checks.*.detail[].share",
    "deal_baseline.D13_reference_candidates[].buyer",
    "deal_baseline.D6_capacity_hints.largest_lot",
)


def needs_translation(text: str) -> bool:
    # Numbers, codes and dates carry no language.
    return bool(re.search(r"[^\W\d_]{2}", text))


def walk(value: Any, path: str) -> Iterator[tuple[str, Any, Any]]:
    """Yield (path, container, key) for every string under value."""
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if isinstance(child, str):
                yield child_path, value, key
            else:
                yield from walk(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            if isinstance(child, str):
                yield f"{path}[]", value, index
            else:
                yield from walk(child, f"{path}[]")


# fnmatch would read "[]" as a character class, so build exact patterns: only "*" is a
# wildcard, and it stands for one key.
TRANSLATABLE_RE = re.compile(
    "|".join(re.escape(p).replace(r"\*", r"[^.\[]+") for p in TRANSLATABLE)
)


def translatable(path: str) -> bool:
    return TRANSLATABLE_RE.fullmatch(path) is not None


def strings(profile: dict[str, Any]) -> Iterator[tuple[Any, Any, str]]:
    for path, container, key in walk(profile, ""):
        text = container[key]
        if path == "name":
            # Translate from the registered name, even after a previous run.
            text = profile.get("legal_name", text)
        if translatable(path) and text and needs_translation(text):
            yield container, key, text


def load_profiles(data_dir: Path) -> dict[Path, list[dict[str, Any]]]:
    return {
        data_dir / name: json.loads((data_dir / name).read_text(encoding="utf-8"))
        for name in PROFILE_FILES
        if (data_dir / name).exists()
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply (or list missing) English translations for data/profiles."
    )
    parser.add_argument(
        "--data-dir", type=Path, default=Path("data"), help="data directory (default: data)"
    )
    parser.add_argument(
        "--extract",
        type=Path,
        help="write untranslated strings to this JSON file instead of applying",
    )
    args = parser.parse_args()

    memory_path = args.data_dir / "translations.json"
    memory: dict[str, str] = (
        json.loads(memory_path.read_text(encoding="utf-8")) if memory_path.exists() else {}
    )
    files = load_profiles(args.data_dir)
    # Text that is already a translation needs nothing (the data was translated before).
    done = set(memory.values())

    if args.extract:
        pending = sorted(
            {
                text
                for profiles in files.values()
                for profile in profiles
                for _, _, text in strings(profile)
                if text not in memory and text not in done
            }
        )
        args.extract.write_text(
            json.dumps(pending, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(f"{len(pending)} untranslated strings written to {args.extract}")
        return 0

    missing: set[str] = set()
    for path, profiles in files.items():
        for profile in profiles:
            profile.setdefault("legal_name", profile["name"])
            for container, key, text in strings(profile):
                if text in memory:
                    container[key] = memory[text]
                elif text not in done:
                    missing.add(text)
        path.write_text(
            json.dumps(profiles, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"Translated {path}")
    if missing:
        print(
            f"{len(missing)} strings have no translation yet; run with --extract.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
