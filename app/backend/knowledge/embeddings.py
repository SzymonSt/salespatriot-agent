"""Query embeddings for RAG and memory recall (contract §3.1).

Queries are embedded with ``gemini-embedding-001`` at 768 dimensions and task
type ``RETRIEVAL_QUERY``, then L2-normalised (reduced-dimension Gemini vectors
are not normalised by the API). Any failure returns ``None``; the RPCs then
fall back to full-text search, so an embedding outage never fails a request.
"""

import asyncio
import logging
import math
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence

from google import genai
from google.genai import types

from app.backend.settings import Settings

EMBEDDING_DIMENSIONS = 768
QUERY_TASK_TYPE = "RETRIEVAL_QUERY"
MAX_QUERY_CHARS = 2_000

logger = logging.getLogger(__name__)

EmbedFunction = Callable[[str], Awaitable[Sequence[float] | None]]


def l2_normalise(values: Sequence[float]) -> list[float] | None:
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm == 0:
        return None
    return [value / norm for value in values]


class QueryEmbedder:
    """Embeds query text with an LRU cache and a hard timeout.

    ``embed_function`` is injectable for tests; by default the google-genai
    async client is used.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = "gemini-embedding-001",
        timeout_seconds: float = 3.0,
        cache_size: int = 512,
        embed_function: EmbedFunction | None = None,
    ) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._cache_size = cache_size
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._client: genai.Client | None = None
        self._embed_function = embed_function
        if embed_function is None and api_key:
            self._client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)),
            )

    @classmethod
    def from_settings(cls, settings: Settings) -> "QueryEmbedder":
        api_key = (
            settings.google_api_key.get_secret_value()
            if settings.google_api_key is not None
            else None
        )
        return cls(
            api_key=api_key,
            model=settings.embedding_model,
            timeout_seconds=settings.embedding_timeout_seconds,
        )

    @property
    def enabled(self) -> bool:
        return self._embed_function is not None or self._client is not None

    async def _call_gemini(self, text: str) -> Sequence[float] | None:
        assert self._client is not None
        response = await self._client.aio.models.embed_content(
            model=self.model,
            contents=text,
            config=types.EmbedContentConfig(
                task_type=QUERY_TASK_TYPE,
                output_dimensionality=EMBEDDING_DIMENSIONS,
            ),
        )
        if not response.embeddings:
            return None
        return response.embeddings[0].values

    async def embed(self, text: str | None) -> list[float] | None:
        """Returns a normalised 768-dimension vector, or ``None`` on any failure."""
        key = " ".join((text or "").split())[:MAX_QUERY_CHARS]
        if not key or not self.enabled:
            return None
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        embed = self._embed_function or self._call_gemini
        try:
            values = await asyncio.wait_for(embed(key), timeout=self.timeout_seconds)
        except Exception as error:  # noqa: BLE001 - any failure falls back to full text
            logger.warning("Query embedding failed (%s); using full-text search", type(error).__name__)
            return None
        if values is None or len(values) != EMBEDDING_DIMENSIONS:
            logger.warning("Query embedding had an unexpected shape; using full-text search")
            return None
        vector = l2_normalise([float(value) for value in values])
        if vector is None:
            return None

        self._cache[key] = vector
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return vector
