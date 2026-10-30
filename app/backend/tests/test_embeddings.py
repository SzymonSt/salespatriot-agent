import asyncio
import math
from types import SimpleNamespace

from app.backend.knowledge.embeddings import EMBEDDING_DIMENSIONS, QueryEmbedder
from app.backend.settings import Settings


def raw_vector() -> list[float]:
    return [3.0, 4.0] + [0.0] * (EMBEDDING_DIMENSIONS - 2)


async def test_vectors_are_normalised_and_cached_by_text() -> None:
    calls: list[str] = []

    async def embed(text: str):
        calls.append(text)
        return raw_vector()

    embedder = QueryEmbedder(api_key=None, embed_function=embed)
    first = await embedder.embed("  9x19   ammunition ")
    second = await embedder.embed("9x19 ammunition")

    assert calls == ["9x19 ammunition"]
    assert first == second
    assert math.isclose(math.sqrt(sum(v * v for v in first)), 1.0)
    assert first[:2] == [0.6, 0.8]


async def test_failures_timeouts_and_wrong_shapes_return_none() -> None:
    async def slow(text: str):
        await asyncio.sleep(1)
        return raw_vector()

    async def broken(text: str):
        raise RuntimeError("quota")

    async def short(text: str):
        return [1.0, 2.0]

    async def zero(text: str):
        return [0.0] * EMBEDDING_DIMENSIONS

    assert await QueryEmbedder(api_key=None, timeout_seconds=0.01, embed_function=slow).embed("q") is None
    assert await QueryEmbedder(api_key=None, embed_function=broken).embed("q") is None
    assert await QueryEmbedder(api_key=None, embed_function=short).embed("q") is None
    assert await QueryEmbedder(api_key=None, embed_function=zero).embed("q") is None
    assert await QueryEmbedder(api_key=None, embed_function=short).embed("") is None


async def test_without_api_key_embedding_is_disabled() -> None:
    embedder = QueryEmbedder.from_settings(Settings(_env_file=None))
    assert embedder.enabled is False
    assert await embedder.embed("anything") is None


async def test_gemini_call_uses_retrieval_query_and_768_dimensions() -> None:
    captured: dict = {}

    async def embed_content(*, model, contents, config):
        captured.update(model=model, contents=contents, config=config)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=raw_vector())])

    embedder = QueryEmbedder.from_settings(Settings(_env_file=None, google_api_key="not-a-real-key"))
    embedder._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(embed_content=embed_content)))

    vector = await embedder.embed("155 mm artillery shells Spain")

    assert vector is not None and len(vector) == EMBEDDING_DIMENSIONS
    assert captured["model"] == "gemini-embedding-001"
    assert captured["contents"] == "155 mm artillery shells Spain"
    assert captured["config"].task_type == "RETRIEVAL_QUERY"
    assert captured["config"].output_dimensionality == EMBEDDING_DIMENSIONS
