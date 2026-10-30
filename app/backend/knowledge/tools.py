"""ADK function tools over the knowledge client (contract §3.6).

The viewer id, role and (optionally) conversation are bound in a closure by the
agent factory. The model never chooses whose data it reads: a supplier agent's
``assess_tender`` always uses its own supplier id, and ``evaluate_quote`` only
reads the conversation the agent is part of. Tool failures are returned to the
model as ``{"error": ...}`` so one failing lookup does not abort the turn.
"""

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, get_args

from app.backend.knowledge.client import KnowledgeClient, KnowledgeError
from app.backend.knowledge.embeddings import QueryEmbedder
from app.backend.knowledge.models import ChunkKind, validate_subject

PURPOSES_BY_ROLE: dict[str, tuple[str, ...]] = {
    "supplier": ("assess_tender", "check_counterparty", "search"),
    "buyer": ("find_suppliers", "evaluate_quote", "check_counterparty", "search"),
}
CHUNK_KINDS = frozenset(get_args(ChunkKind))
REGULATORY_NOTE = "Defaults from the product ontology crosswalk, not a licence decision."


def collect_refs(value: Any, *, limit: int = 300) -> list[str]:
    """Every ``ref`` / ``refs`` / ``evidence_refs`` string in a pack or tool result, in order."""
    found: dict[str, None] = {}

    def add(candidate: Any) -> None:
        if isinstance(candidate, str) and candidate.strip() and len(found) < limit:
            found.setdefault(candidate.strip(), None)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                if key == "ref":
                    add(child)
                elif key in {"refs", "evidence_refs", "result_refs"} and isinstance(child, list):
                    for item in child:
                        add(item)
                else:
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return list(found)


@dataclass
class ToolCallRecord:
    tool: str
    args: dict[str, Any]
    result_refs: list[str]
    latency_ms: int
    error: str | None = None


@dataclass
class ToolCallRecorder:
    """Collects tool calls of one agent turn so the orchestrator can log them."""

    calls: list[ToolCallRecord] = field(default_factory=list)

    def record(self, call: ToolCallRecord) -> None:
        self.calls.append(call)

    def drain(self) -> list[ToolCallRecord]:
        calls, self.calls = self.calls, []
        return calls

    @property
    def seen_refs(self) -> set[str]:
        return {ref for call in self.calls for ref in call.result_refs}


@dataclass
class KnowledgeToolContext:
    client: KnowledgeClient
    embedder: QueryEmbedder | None
    role: str
    viewer_id: str
    conversation_id: str | None = None
    recorder: ToolCallRecorder | None = None
    limit: int = 8


def _clean(**values: Any) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "", [])}


def build_knowledge_tools(context: KnowledgeToolContext) -> list[Callable[..., Awaitable[dict[str, Any]]]]:
    if context.role not in PURPOSES_BY_ROLE:
        raise ValueError(f"Unknown agent role {context.role!r}")
    allowed_purposes = PURPOSES_BY_ROLE[context.role]

    async def run(tool: str, args: dict[str, Any], call: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        started = time.perf_counter()
        error: str | None = None
        try:
            result = await call()
        except (KnowledgeError, ValueError) as failure:
            error = str(failure)
            result = {"error": error}
        refs = collect_refs(result) if error is None else []
        if context.recorder is not None:
            context.recorder.record(
                ToolCallRecord(
                    tool=tool,
                    args=args,
                    result_refs=refs,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    error=error,
                )
            )
        return result

    async def embed(query: str | None) -> list[float] | None:
        if context.embedder is None or not query:
            return None
        return await context.embedder.embed(query)

    async def get_context(
        purpose: str,
        publication_number: str | None = None,
        supplier_id: str | None = None,
        buyer_id: str | None = None,
        family_id: str | None = None,
        calibre: str | None = None,
        country: str | None = None,
        message_id: str | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        """Returns the platform's context pack: facts, candidates, evidence, price anchors,
        regulatory defaults, memory, warnings and refs. Cite the refs you rely on.

        Args:
          purpose: assess_tender (supplier), find_suppliers (buyer), evaluate_quote (buyer),
            check_counterparty, or search.
          publication_number: TED publication number of the tender, e.g. "750233-2026".
          supplier_id: supplier id for check_counterparty (assess_tender always uses your own id).
          buyer_id: buyer id for check_counterparty.
          family_id: product family id, for find_suppliers without a tender.
          calibre: canonical calibre code, e.g. "9x19".
          country: ISO 3166-1 alpha-3 country code, e.g. "POL".
          message_id: id of the quote message, for evaluate_quote in this conversation.
          query: optional free-text question for the evidence search.
        """
        subject = _clean(
            publication_number=publication_number,
            supplier_id=supplier_id,
            buyer_id=buyer_id,
            family_id=family_id,
            calibre=calibre,
            country=country.upper() if country else None,
            message_id=message_id,
        )
        args = {"purpose": purpose, **subject, **_clean(query=query)}

        async def call() -> dict[str, Any]:
            if purpose not in allowed_purposes:
                raise ValueError(f"purpose {purpose!r} is not available to a {context.role} agent")
            if purpose == "assess_tender":
                subject["supplier_id"] = context.viewer_id
            if purpose == "evaluate_quote":
                if context.conversation_id is None:
                    raise ValueError("evaluate_quote is only available inside a conversation")
                subject["conversation_id"] = context.conversation_id
            validate_subject(purpose, subject, query)
            return await context.client.context(
                role=context.role,
                purpose=purpose,
                subject=subject,
                query_embedding=await embed(query),
                query_text=query,
                viewer=context.viewer_id,
                limit=context.limit,
            )

        return await run("get_context", args, call)

    async def search_knowledge(
        query: str,
        kinds: list[str] | None = None,
        family_id: str | None = None,
        calibre: str | None = None,
        cpv_prefix: str | None = None,
        country: str | None = None,
    ) -> dict[str, Any]:
        """Searches tenders, awards, contracts, profiles, catalogue items, past deals and the
        platform knowledge base. Returns hits with ref, kind, title, snippet, facts and score.

        Args:
          query: what to look for, in any language.
          kinds: optional subset of tender, award, contract, supplier_profile, buyer_profile,
            catalogue_item, conversation, kb_doc, pattern.
          family_id: optional product family id filter.
          calibre: optional canonical calibre filter, e.g. "5.56x45".
          cpv_prefix: optional CPV code prefix (digits), e.g. "3533".
          country: optional ISO 3166-1 alpha-3 country code.
        """
        args = _clean(query=query, kinds=kinds, family_id=family_id, calibre=calibre,
                      cpv_prefix=cpv_prefix, country=country)

        async def call() -> dict[str, Any]:
            if not query or not query.strip():
                raise ValueError("query must not be empty")
            unknown = set(kinds or []) - CHUNK_KINDS
            if unknown:
                raise ValueError(f"unknown kinds: {sorted(unknown)}")
            if cpv_prefix and not re.fullmatch(r"\d{2,8}", cpv_prefix):
                raise ValueError("cpv_prefix must be 2 to 8 digits")
            hits = await context.client.search(
                query_embedding=await embed(query),
                query_text=query,
                kinds=kinds or None,
                family_id=family_id,
                calibre=calibre,
                cpv_prefix=cpv_prefix,
                country=country.upper() if country else None,
                viewer=context.viewer_id,
                limit=context.limit,
            )
            return {"hits": hits}

        return await run("search_knowledge", args, call)

    async def recall_memory(
        query: str,
        family_id: str | None = None,
        country: str | None = None,
        counterparty: str | None = None,
    ) -> dict[str, Any]:
        """Recalls lessons and past episodes relevant to this deal (validated lessons are
        platform-wide; episodes only for the parties). Each memory has a ref.

        Args:
          query: what the memory should be about.
          family_id: optional product family id.
          country: optional ISO 3166-1 alpha-3 country code.
          counterparty: optional counterparty id or name.
        """
        args = _clean(query=query, family_id=family_id, country=country, counterparty=counterparty)

        async def call() -> dict[str, Any]:
            memories = await context.client.recall(
                query_embedding=await embed(query),
                role=context.role,
                family_id=family_id,
                country=country.upper() if country else None,
                counterparty=counterparty,
                viewer=context.viewer_id,
                limit=min(context.limit, 5),
            )
            return {"memories": memories}

        return await run("recall_memory", args, call)

    async def get_family_schema(family_id: str) -> dict[str, Any]:
        """Returns the attribute schema of a product family (listing mode): which attributes
        exist, which are required, and their allowed values.

        Args:
          family_id: product family id, e.g. "ammunition.small_calibre".
        """

        async def call() -> dict[str, Any]:
            schema = await context.client.family_schema(family_id)
            if schema is None:
                raise ValueError(f"unknown product family {family_id!r}")
            return {"family_id": family_id, "schema": schema}

        return await run("get_family_schema", {"family_id": family_id}, call)

    async def regulatory_profile(family_id: str, calibre: str | None = None) -> dict[str, Any]:
        """Returns regulatory defaults for a product family and calibre: EU Military List,
        NATO FSC, HS/CN, Firearms Directive, ADR, STANAG. Defaults only, never a licence decision.

        Args:
          family_id: product family id, e.g. "ammunition.small_calibre".
          calibre: optional canonical calibre code, e.g. "9x19".
        """

        async def call() -> dict[str, Any]:
            profile = await context.client.regulatory_profile(family_id, calibre)
            if profile is None:
                raise ValueError(f"no regulatory profile for {family_id!r}")
            return {"family_id": family_id, "calibre": calibre, "profile": profile, "note": REGULATORY_NOTE}

        return await run("regulatory_profile", _clean(family_id=family_id, calibre=calibre), call)

    return [get_context, search_knowledge, recall_memory, get_family_schema, regulatory_profile]
