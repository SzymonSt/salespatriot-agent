#!/usr/bin/env python3
"""Turn data/families.yaml into idempotent SQL for the product_* tables in Supabase.

    python3 seed_product_taxonomy.py > /tmp/seed.sql      # then run it (psql, Supabase SQL editor or MCP)
    python3 seed_product_taxonomy.py --check              # validate the YAML only

Families missing from the YAML are marked deprecated, never deleted (items may point at them).
Child rows (attributes, crosswalk, keywords) of every family in the YAML are replaced.
Design: knowledge_base/25-product-families.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

TAXONOMY = Path(__file__).parent / "data" / "families.yaml"
DATA_TYPES = {"text", "number", "integer", "boolean", "enum", "enum_list", "text_list", "calibre"}
RELATIONS = {"exact", "broad", "narrow", "candidate", "related"}
SCHEMES = {"cpv", "cpvsuppl", "eu_ml", "fsc", "hs", "fd_2021_555", "adr", "stanag", "unspsc", "wassenaar"}


def q(value) -> str:
    """SQL literal."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def arr(values) -> str:
    if not values:
        return "'{}'::text[]"
    return "array[" + ", ".join(q(v) for v in values) + "]::text[]"


def norm_alias(text: str) -> str:
    """Same normalisation as public.product_normalise_calibre."""
    return str(text).lower().replace(",", ".").replace("×", "x").replace(" ", "")


def attribute_links(spec) -> list[tuple[str, dict]]:
    if not spec:
        return []
    if isinstance(spec, list):
        return [(key, {}) for key in spec]
    return [(key, opts or {}) for key, opts in spec.items()]


def validate(t: dict) -> list[str]:
    errors = []
    attrs = t.get("attributes", {})
    for key, a in attrs.items():
        if a.get("type") not in DATA_TYPES:
            errors.append(f"attribute {key}: bad type {a.get('type')}")
        if a.get("type") in {"enum", "enum_list"} and not a.get("values"):
            errors.append(f"attribute {key}: enum without values")
    ids = set()
    for cls in t["classes"]:
        nodes = [cls] + cls.get("families", [])
        for node in nodes:
            if node["id"] in ids:
                errors.append(f"duplicate id {node['id']}")
            ids.add(node["id"])
            for key, opts in attribute_links(node.get("attributes")):
                if key not in attrs:
                    errors.append(f"{node['id']}: unknown attribute {key}")
                allowed = set(attrs.get(key, {}).get("values") or [])
                narrowed = set(opts.get("values") or [])
                if narrowed and allowed and not narrowed <= allowed:
                    errors.append(f"{node['id']}.{key}: values not in the global enum: {sorted(narrowed - allowed)}")
            for rel in (node.get("cpv") or {}):
                if rel not in RELATIONS:
                    errors.append(f"{node['id']}: bad cpv relation {rel}")
        for fam in cls.get("families", []):
            if not fam["id"].startswith(cls["id"] + "."):
                errors.append(f"{fam['id']}: id must start with '{cls['id']}.'")
    for c in t.get("calibres", []):
        if c.get("family") and c["family"] not in ids:
            errors.append(f"calibre {c['code']}: unknown family {c['family']}")
    sources = t.get("sources") or {}
    for fid, rows in (t.get("crosswalk") or {}).items():
        if fid not in ids:
            errors.append(f"crosswalk: unknown family {fid}")
        for row in rows:
            if len(row) != 6:
                errors.append(f"crosswalk {fid}: row needs 6 fields: {row}")
                continue
            scheme, _code, relation, status, source, _note = row
            if scheme not in SCHEMES:
                errors.append(f"crosswalk {fid}: bad scheme {scheme}")
            if relation not in RELATIONS:
                errors.append(f"crosswalk {fid}: bad relation {relation}")
            if status not in {"V", "V2", "I"}:
                errors.append(f"crosswalk {fid}: bad status {status}")
            if source not in sources:
                errors.append(f"crosswalk {fid}: unknown source {source}")
    return errors


def upsert(table: str, cols: list[str], rows: list[list], key: list[str] | None) -> str:
    """One multi-row insert per table; on conflict update every non-key column."""
    if not rows:
        return ""
    if key:  # a multi-row upsert must not touch the same key twice
        idx = [cols.index(k) for k in key]
        rows = list({tuple(r[i] for i in idx): r for r in reversed(rows)}.values())[::-1]
    values = ",\n  ".join("(" + ", ".join(r) + ")" for r in rows)
    sql = f"insert into public.{table} ({', '.join(cols)}) values\n  {values}\n"
    if key:
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in key)
        sql += f"on conflict ({', '.join(key)}) do update set {updates}" if updates else f"on conflict ({', '.join(key)}) do nothing"
    return sql + ";"


def build_sql(t: dict) -> str:
    v = int(t["taxonomy_version"])
    nodes = []
    for ci, cls in enumerate(t["classes"]):
        nodes.append((cls, None, 1, ci * 100))
        for fi, fam in enumerate(cls.get("families", [])):
            nodes.append((fam, cls, 2, ci * 100 + fi + 1))
    all_ids = [n["id"] for n, *_ in nodes]

    versions = [[str(v), q("data/families.yaml"), q(t.get("notes"))]]
    attr_defs = [
        [q(key), q(a["label"]), q(a["type"]), q(a.get("unit")), arr(a.get("values")) if a.get("values") else "null", q(a["description"])]
        for key, a in t["attributes"].items()
    ]
    families = []
    links, crosswalk, keywords = [], [], []
    for node, parent, level, order in nodes:
        fid = node["id"]
        in_focus = node.get("in_focus", parent.get("in_focus", True) if parent else True)
        families.append([
            q(fid), q(parent["id"] if parent else None), str(level), q(node["name"]),
            q(node.get("description") or node["name"]), arr(node.get("includes")), arr(node.get("excludes")),
            q(in_focus), q(node.get("review_flag")), q(node.get("status", "active")), str(order), str(v),
            arr(node.get("rules")), q(node.get("evidence")),
        ])
        for i, (key, opts) in enumerate(attribute_links(node.get("attributes"))):
            links.append([q(fid), q(key), q(bool(opts.get("required"))), q(bool(opts.get("match_key"))),
                          arr(opts.get("values")) if opts.get("values") else "null", str(i)])
        for relation, codes in (node.get("cpv") or {}).items():
            for code in codes:
                crosswalk.append([q(fid), "'cpv'", q(code), "null", q(relation), "true", "null", "null"])
        for scheme, code, relation, status, source, note in (t.get("crosswalk") or {}).get(fid, []):
            note = f"[{status}] {note}".strip() if note else f"[{status}]"
            crosswalk.append([q(fid), q(scheme), q(str(code)), "null", q(relation), q(status == "V"), q(source), q(note)])
        if node.get("keywords"):
            keywords.append(f"({q(fid)}, {arr(list(dict.fromkeys(node['keywords'])))})")
    noise = [[q(n["cpv"]), q(n["label"]), q(n["rule"]), q(n.get("evidence"))] for n in t.get("noise_rules", [])]
    tags = [[q(code), q(tag["label"]), q(tag["when"])] for code, tag in (t.get("cpvsuppl_tags") or {}).items()]
    calibres, aliases = [], {}
    for c in t.get("calibres", []):
        calibres.append([q(c["code"]), q(c["label"]), q(c["kind"]), q(c.get("bore_mm")), q(c.get("case_mm")),
                         q(c.get("standard")), arr(c.get("related")), q(c.get("family"))])
        for alias in [c["code"], c["label"], *(c.get("aliases") or [])]:
            aliases.setdefault(norm_alias(alias), c["code"])

    out = [
        "begin;",
        upsert("product_taxonomy_versions", ["version", "source", "notes"], versions, ["version"]),
        upsert("product_attribute_defs", ["attr_key", "label", "data_type", "unit", "enum_values", "description"],
               attr_defs, ["attr_key"]),
        upsert("product_families", ["id", "parent_id", "level", "name", "description", "includes", "excludes", "in_focus",
                                    "review_flag", "status", "sort_order", "taxonomy_version", "rules", "evidence"],
               families, ["id"]),
        f"update public.product_families set status = 'deprecated' where id <> all ({arr(all_ids)}) and status <> 'deprecated';",
        f"delete from public.product_family_attributes where family_id = any ({arr(all_ids)});",
        f"delete from public.product_family_crosswalk where family_id = any ({arr(all_ids)});",
        f"delete from public.product_family_keywords where family_id = any ({arr(all_ids)});",
        upsert("product_family_attributes", ["family_id", "attr_key", "required", "is_match_key", "enum_values", "sort_order"],
               links, None),
        upsert("product_family_crosswalk", ["family_id", "scheme", "code", "label", "relation", "verified", "source_url", "note"],
               crosswalk, ["family_id", "scheme", "code"]),
        "update public.product_family_crosswalk set source_url = "
        "'https://github.com/OP-TED/eForms-SDK/blob/1.15.1/codelists/cpv.gc' where scheme = 'cpv' and source_url is null;",
        # crosswalk rows carry a source key; expand it to the URL once
        ("update public.product_family_crosswalk c set source_url = s.url from (values\n  "
         + ",\n  ".join(f"({q(k)}, {q(u)})" for k, u in (t.get("sources") or {}).items())
         + "\n) s (k, url) where c.source_url = s.k;") if t.get("sources") else "",
        ("insert into public.product_family_keywords (family_id, pattern)\nselect v.f, p from (values\n  "
         + ",\n  ".join(keywords) + "\n) v (f, ps), unnest(v.ps) p on conflict do nothing;") if keywords else "",
        upsert("product_noise_rules", ["cpv_code", "label", "rule", "evidence"], noise, ["cpv_code"]),
        upsert("product_cpvsuppl_tags", ["code", "label", "rule"], tags, ["code"]),
        upsert("product_calibres", ["code", "label", "kind", "bore_mm", "case_length_mm", "standard", "related_codes",
                                    "default_family_id"], calibres, ["code"]),
        "delete from public.product_value_aliases where attr_key = 'calibre';",
        upsert("product_value_aliases", ["attr_key", "alias", "canonical"],
               [["'calibre'", q(a), q(code)] for a, code in sorted(aliases.items())], ["attr_key", "alias"]),
        "commit;",
    ]
    return "\n\n".join(s for s in out if s) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file", type=Path, default=TAXONOMY)
    parser.add_argument("--check", action="store_true", help="validate only")
    args = parser.parse_args()

    taxonomy = yaml.safe_load(args.file.read_text())
    errors = validate(taxonomy)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    if args.check:
        fams = sum(len(c.get("families", [])) for c in taxonomy["classes"])
        print(f"ok: {len(taxonomy['classes'])} classes, {fams} families, {len(taxonomy['attributes'])} attributes, "
              f"{len(taxonomy.get('calibres', []))} calibres", file=sys.stderr)
        return 0
    sys.stdout.write(build_sql(taxonomy))
    return 0


if __name__ == "__main__":
    sys.exit(main())
