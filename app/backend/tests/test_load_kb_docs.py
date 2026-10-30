from app.backend.knowledge.load_kb_docs import (
    MAX_CONTENT_CHARS,
    build_chunks,
    collect_chunks,
    split_sections,
)
from app.backend.settings import Settings

DOC = """# Self-learning RAG

Intro paragraph.

## How accuracy grows

Text one.

```sql
## not a heading inside a fence
select 1;
```

## How accuracy grows

Second section with the same heading.
"""


def test_sections_split_on_headings_outside_code_fences() -> None:
    sections = split_sections(DOC)
    assert [heading for heading, _ in sections] == ["Self-learning RAG", "How accuracy grows", "How accuracy grows"]
    assert "## not a heading inside a fence" in sections[1][1]


def test_chunks_have_stable_unique_keys_and_kb_doc_content(tmp_path) -> None:
    path = tmp_path / "23-self-learning-rag.md"
    path.write_text(DOC, encoding="utf-8")

    chunks = build_chunks(path)
    assert [c.source_key for c in chunks] == [
        "kb:23-self-learning-rag#self-learning-rag",
        "kb:23-self-learning-rag#how-accuracy-grows",
        "kb:23-self-learning-rag#how-accuracy-grows-2",
    ]
    assert chunks[1].content.startswith("KB 23-self-learning-rag.md · How accuracy grows\n")
    assert build_chunks(path) == chunks


def test_long_sections_are_split_into_parts_within_the_limit(tmp_path) -> None:
    paragraph = "word " * 120
    path = tmp_path / "99-long.md"
    path.write_text("## Long\n\n" + "\n\n".join([paragraph] * 12), encoding="utf-8")

    chunks = build_chunks(path)
    assert len(chunks) > 1
    assert all(len(c.content) <= MAX_CONTENT_CHARS for c in chunks)
    assert chunks[0].source_key == "kb:99-long#long~1"
    assert chunks[0].facts["parts"] == len(chunks)


def test_repository_knowledge_base_builds_without_duplicates() -> None:
    chunks = collect_chunks(Settings(_env_file=None).knowledge_base_dir)
    assert chunks
    assert all(len(c.content) <= MAX_CONTENT_CHARS for c in chunks)
