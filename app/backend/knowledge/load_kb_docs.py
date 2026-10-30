"""Load ``knowledge_base/*.md`` into ``public.kb_chunks`` as ``kb_doc`` chunks (plan §5 WS1 1.2, WS5 5.5).

Usage, from the repository root:

    uv run --project app/backend python -m app.backend.knowledge.load_kb_docs [--dry-run] [--no-prune]

Each file is split on ``##`` headings (outside code fences). Sections longer than the 2,000-character
content limit are split on paragraph boundaries into parts. Rows are upserted on
``(kind, source_key, layer, snapshot_id)`` with ``layer = 'platform'`` and ``visibility = 'public'``;
unchanged rows are not touched, so their embeddings stay valid. ``kb_doc`` rows whose section no
longer exists are deleted (``--no-prune`` keeps them). Running it twice changes nothing the second time.

Needs ``DATABASE_URL`` with a role that may write ``kb_chunks`` (the ``postgres`` pooler user or the
service role). Exits with status 2 when ``kb_chunks`` does not exist yet (WS1).
"""

import argparse
import asyncio
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.backend.database import create_database_engine
from app.backend.settings import get_settings

MAX_CONTENT_CHARS = 2_000
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^##\s+(.+?)\s*#*\s*$")
TITLE = re.compile(r"^#\s+(.+?)\s*$")


@dataclass(frozen=True)
class KbChunk:
    source_key: str
    title: str
    content: str
    facts: dict[str, Any]


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[`*_\[\]()]", "", value.lower())
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "section"


def split_sections(markdown: str) -> list[tuple[str, str]]:
    """``(heading, body)`` pairs. Text before the first ``##`` is ``intro`` (or the ``#`` title)."""
    sections: list[tuple[str, list[str]]] = [("", [])]
    in_fence = False
    for line in markdown.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else HEADING.match(line)
        if match:
            sections.append((match.group(1).strip(), []))
        else:
            sections[-1][1].append(line)

    result: list[tuple[str, str]] = []
    for index, (heading, lines) in enumerate(sections):
        body = "\n".join(lines).strip()
        if index == 0:
            title = next((TITLE.match(line) for line in lines if TITLE.match(line)), None)
            heading = title.group(1).strip() if title else "Introduction"
            body = "\n".join(line for line in lines if not TITLE.match(line)).strip()
        if body:
            result.append((heading, body))
    return result


def split_body(body: str, limit: int) -> list[str]:
    """Splits on blank lines (then on lines, then hard) so each part fits ``limit``."""
    if len(body) <= limit:
        return [body]
    parts: list[str] = []
    current = ""
    for block in re.split(r"\n\s*\n", body):
        pieces = [block] if len(block) <= limit else block.splitlines() or [block]
        for piece in pieces:
            while len(piece) > limit:
                if current:
                    parts.append(current)
                    current = ""
                parts.append(piece[:limit])
                piece = piece[limit:]
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                parts.append(current)
                current = piece
    if current:
        parts.append(current)
    return [part.strip() for part in parts if part.strip()]


def build_chunks(path: Path) -> list[KbChunk]:
    stem = path.stem
    chunks: list[KbChunk] = []
    seen: dict[str, int] = {}
    for heading, body in split_sections(path.read_text(encoding="utf-8")):
        slug = slugify(heading)
        seen[slug] = seen.get(slug, 0) + 1
        if seen[slug] > 1:
            slug = f"{slug}-{seen[slug]}"
        header = f"KB {path.name} · {heading}\n"
        parts = split_body(body, MAX_CONTENT_CHARS - len(header) - 12)
        for number, part in enumerate(parts, start=1):
            suffix = f"~{number}" if len(parts) > 1 else ""
            label = f" (part {number}/{len(parts)})" if len(parts) > 1 else ""
            chunks.append(
                KbChunk(
                    source_key=f"kb:{stem}#{slug}{suffix}",
                    title=f"{stem} · {heading}{label}"[:300],
                    content=f"KB {path.name} · {heading}{label}\n{part}"[:MAX_CONTENT_CHARS],
                    facts={"file": path.name, "heading": heading, "part": number, "parts": len(parts)},
                )
            )
    return chunks


def collect_chunks(directory: Path) -> list[KbChunk]:
    chunks: list[KbChunk] = []
    for path in sorted(directory.glob("*.md")):
        chunks.extend(build_chunks(path))
    keys = [chunk.source_key for chunk in chunks]
    duplicates = {key for key in keys if keys.count(key) > 1}
    if duplicates:
        raise ValueError(f"Duplicate kb_doc source keys: {sorted(duplicates)}")
    return chunks


UPSERT_SQL = text(
    "insert into public.kb_chunks as k (kind, source_key, ref, title, content, facts, layer, "
    "snapshot_id, visibility, updated_at) values ('kb_doc', :source_key, :source_key, :title, "
    ":content, cast(:facts as jsonb), 'platform', null, 'public', now()) "
    "on conflict (kind, source_key, layer, snapshot_id) do update set "
    "ref = excluded.ref, title = excluded.title, content = excluded.content, facts = excluded.facts, "
    "visibility = excluded.visibility, updated_at = now() "
    "where k.title is distinct from excluded.title or k.content is distinct from excluded.content "
    "or k.facts is distinct from excluded.facts or k.visibility is distinct from excluded.visibility "
    "returning (xmax = 0) as inserted"
)
PRUNE_SQL = text(
    "delete from public.kb_chunks where kind = 'kb_doc' and layer = 'platform' "
    "and not (source_key = any(cast(:keys as text[])))"
)


async def load(engine: AsyncEngine, chunks: list[KbChunk], *, prune: bool) -> dict[str, int]:
    counts = {"chunks": len(chunks), "inserted": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    async with engine.begin() as connection:
        exists = (await connection.execute(text("select to_regclass('public.kb_chunks') is not null"))).scalar()
        if not exists:
            raise LookupError("public.kb_chunks does not exist yet (WS1); nothing was loaded")
        for chunk in chunks:
            row = (
                await connection.execute(
                    UPSERT_SQL,
                    {
                        "source_key": chunk.source_key,
                        "title": chunk.title,
                        "content": chunk.content,
                        "facts": json.dumps(chunk.facts, ensure_ascii=False),
                    },
                )
            ).first()
            if row is None:
                counts["unchanged"] += 1
            elif row[0]:
                counts["inserted"] += 1
            else:
                counts["updated"] += 1
        if prune:
            result = await connection.execute(PRUNE_SQL, {"keys": [chunk.source_key for chunk in chunks]})
            counts["deleted"] = result.rowcount or 0
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", type=Path, default=None, help="knowledge base directory")
    parser.add_argument("--dry-run", action="store_true", help="print the chunks, write nothing")
    parser.add_argument("--no-prune", action="store_true", help="keep kb_doc rows of removed sections")
    args = parser.parse_args(argv)

    settings = get_settings()
    directory = args.dir or settings.knowledge_base_dir
    chunks = collect_chunks(directory)
    if args.dry_run:
        for chunk in chunks:
            print(f"{chunk.source_key}\t{len(chunk.content)}\t{chunk.title}")
        print(json.dumps({"chunks": len(chunks), "files": len(list(directory.glob('*.md')))}))
        return 0

    engine = create_database_engine(settings)
    if engine is None:
        print("DATABASE_URL is unset or still contains a placeholder", file=sys.stderr)
        return 2

    async def run() -> dict[str, int]:
        try:
            return await load(engine, chunks, prune=not args.no_prune)
        finally:
            await engine.dispose()

    try:
        counts = asyncio.run(run())
    except LookupError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(counts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
