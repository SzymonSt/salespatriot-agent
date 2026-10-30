"""Thin async client over the knowledge, conversation and memory RPCs (contract §3.3–§3.5).

Logic lives in SQL; this class only binds parameters and maps database errors:

- RPC or table not deployed yet, or database unreachable → ``KnowledgeUnavailableError`` (HTTP 503)
- the database rejected the call (state machine, constraint) → ``KnowledgeRejectedError`` (409)
- invalid parameter value                                       → ``KnowledgeInvalidError`` (422)

``DATABASE_URL`` must use a role allowed to execute the RPCs (the ``postgres``
pooler user or the service role); they are granted to ``service_role`` only.
"""

import json
import re
from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

_UNAVAILABLE_SQLSTATES = {"42883", "42P01", "3F000", "42704"}
_STATE_MACHINE_MESSAGES = ("illegal transition", "is closed")
_ERROR_PREFIX = re.compile(r"^<class '[^']+'>:\s*")


class KnowledgeError(RuntimeError):
    """Base class; ``str(error)`` is safe to return to API callers."""


class KnowledgeUnavailableError(KnowledgeError):
    pass


class KnowledgeRejectedError(KnowledgeError):
    pass


class KnowledgeInvalidError(KnowledgeError):
    pass


class KnowledgeNotFoundError(KnowledgeError):
    pass


class KnowledgeForbiddenError(KnowledgeError):
    pass


def vector_literal(values: Sequence[float] | None) -> str | None:
    if values is None:
        return None
    return "[" + ",".join(format(float(value), ".8g") for value in values) + "]"


def _json_param(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _as_json(value: Any) -> Any:
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def sqlstate_of(error: BaseException) -> str | None:
    orig = getattr(error, "orig", None)
    for candidate in (orig, getattr(orig, "__cause__", None), error):
        code = getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
        if isinstance(code, str):
            return code
    return None


def _database_message(error: BaseException) -> str:
    orig = getattr(error, "orig", None)
    cause = getattr(orig, "__cause__", None)
    message = getattr(cause, "message", None) or (str(orig) if orig is not None else str(error))
    message = _ERROR_PREFIX.sub("", str(message)).splitlines()[0] if message else ""
    return " ".join(message.split())[:300] or "database error"


def translate_database_error(error: SQLAlchemyError, operation: str) -> KnowledgeError:
    code = sqlstate_of(error)
    message = _database_message(error)
    if code in _UNAVAILABLE_SQLSTATES:
        return KnowledgeUnavailableError(f"{operation} is not available in the database yet ({message})")
    if code == "42501":
        if message.lower().startswith("permission denied"):
            return KnowledgeUnavailableError(f"{operation} is not executable by the DATABASE_URL role ({message})")
        return KnowledgeForbiddenError(f"{operation} was refused: {message}")
    if code == "P0002":
        return KnowledgeNotFoundError(message)
    if code and (
        code == "P0001"
        or code.startswith(("23", "40"))
        or (code.startswith("22") and any(m in message for m in _STATE_MACHINE_MESSAGES))
    ):
        return KnowledgeRejectedError(f"{operation} was rejected: {message}")
    if code and code.startswith("22"):
        return KnowledgeInvalidError(f"{operation} received an invalid value: {message}")
    if not isinstance(error, DBAPIError) or error.connection_invalidated or code is None:
        return KnowledgeUnavailableError("Supabase PostgreSQL connection failed")
    return KnowledgeRejectedError(f"{operation} failed: {message}")


class KnowledgeClient:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def _scalar(self, operation: str, sql: str, params: dict[str, Any]) -> Any:
        try:
            async with self._engine.begin() as connection:
                result = await connection.execute(text(sql), params)
                return _as_json(result.scalar())
        except SQLAlchemyError as error:
            raise translate_database_error(error, operation) from error
        except OSError as error:
            raise KnowledgeUnavailableError("Supabase PostgreSQL connection failed") from error

    # ------------------------------------------------------------------ RAG and context

    async def context(
        self,
        *,
        role: str,
        purpose: str,
        subject: dict[str, Any],
        query_embedding: Sequence[float] | None = None,
        query_text: str | None = None,
        viewer: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        pack = await self._scalar(
            "agent_context",
            "select public.agent_context("
            "cast(:role as text), cast(:purpose as text), cast(cast(:subject as text) as jsonb), "
            "cast(cast(:embedding as text) as extensions.vector), cast(:query as text), "
            "cast(:viewer as text), cast(:limit as integer))",
            {
                "role": role,
                "purpose": purpose,
                "subject": _json_param(subject),
                "embedding": vector_literal(query_embedding),
                "query": query_text,
                "viewer": viewer,
                "limit": limit,
            },
        )
        return pack if isinstance(pack, dict) else {}

    async def search(
        self,
        *,
        query_embedding: Sequence[float] | None = None,
        query_text: str | None = None,
        kinds: Sequence[str] | None = None,
        family_id: str | None = None,
        calibre: str | None = None,
        cpv_prefix: str | None = None,
        country: str | None = None,
        viewer: str | None = None,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        hits = await self._scalar(
            "kb_search",
            "select public.kb_search("
            "cast(cast(:embedding as text) as extensions.vector), cast(:query as text), "
            "cast(:kinds as text[]), cast(:family_id as text), cast(:calibre as text), "
            "cast(:cpv_prefix as text), cast(:country as text), cast(:viewer as text), "
            "cast(:limit as integer))",
            {
                "embedding": vector_literal(query_embedding),
                "query": query_text,
                "kinds": list(kinds) if kinds else None,
                "family_id": family_id,
                "calibre": calibre,
                "cpv_prefix": cpv_prefix,
                "country": country,
                "viewer": viewer,
                "limit": limit,
            },
        )
        return hits if isinstance(hits, list) else []

    async def family_schema(self, family_id: str) -> dict[str, Any] | None:
        schema = await self._scalar(
            "product_family_schema",
            "select public.product_family_schema(cast(:family_id as text), 'listing')",
            {"family_id": family_id},
        )
        return schema if isinstance(schema, dict) else None

    async def regulatory_profile(self, family_id: str, calibre: str | None) -> dict[str, Any] | None:
        profile = await self._scalar(
            "ui_regulatory_profile",
            "select public.ui_regulatory_profile(cast(:family_id as text), cast(:calibre as text))",
            {"family_id": family_id, "calibre": calibre},
        )
        return profile if isinstance(profile, dict) else None

    # ------------------------------------------------------------------ memory

    async def recall(
        self,
        *,
        query_embedding: Sequence[float] | None = None,
        role: str | None = None,
        family_id: str | None = None,
        country: str | None = None,
        counterparty: str | None = None,
        viewer: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        memories = await self._scalar(
            "memory_recall",
            "select public.memory_recall("
            "cast(cast(:embedding as text) as extensions.vector), cast(:role as text), "
            "cast(:family_id as text), cast(:country as text), cast(:counterparty as text), "
            "cast(:viewer as text), cast(:limit as integer))",
            {
                "embedding": vector_literal(query_embedding),
                "role": role,
                "family_id": family_id,
                "country": country,
                "counterparty": counterparty,
                "viewer": viewer,
                "limit": limit,
            },
        )
        return memories if isinstance(memories, list) else []

    async def remember(self, entry: dict[str, Any]) -> str:
        memory_id = await self._scalar(
            "memory_write",
            "select public.memory_write(cast(cast(:entry as text) as jsonb))",
            {"entry": _json_param(entry)},
        )
        return str(memory_id)

    async def memory_feedback(self, memory_id: str, helpful: bool) -> dict[str, Any] | None:
        """``{ref, helpful, unhelpful}``, or ``None`` when the entry does not exist."""
        row = await self._scalar(
            "memory_feedback",
            "select public.memory_feedback(cast(:id as uuid), cast(:helpful as boolean))",
            {"id": memory_id, "helpful": helpful},
        )
        return row if isinstance(row, dict) else None

    # ------------------------------------------------------------------ conversations

    async def create_conversation(
        self,
        *,
        publication_number: str,
        buyer_id: str,
        supplier_id: str,
        family_id: str | None = None,
        buyer_mandate: dict[str, Any],
        supplier_mandate: dict[str, Any],
        demo: bool,
    ) -> dict[str, Any]:
        row = await self._scalar(
            "agent_conversations insert",
            "insert into public.agent_conversations as c "
            "(publication_number, buyer_id, supplier_id, family_id, status, "
            "buyer_mandate, supplier_mandate, demo) values ("
            "cast(:publication_number as text), cast(:buyer_id as text), cast(:supplier_id as text), "
            "cast(:family_id as text), 'open', cast(cast(:buyer_mandate as text) as jsonb), "
            "cast(cast(:supplier_mandate as text) as jsonb), cast(:demo as boolean)) "
            "returning to_jsonb(c)",
            {
                "publication_number": publication_number,
                "buyer_id": buyer_id,
                "supplier_id": supplier_id,
                "family_id": family_id,
                "buyer_mandate": _json_param(buyer_mandate),
                "supplier_mandate": _json_param(supplier_mandate),
                "demo": demo,
            },
        )
        if not isinstance(row, dict):
            raise KnowledgeRejectedError("agent_conversations insert returned no row")
        return row

    async def append_message(self, conversation_id: str, message: dict[str, Any]) -> dict[str, Any]:
        """Appends through ``agent_message_append`` (seq + state machine).

        Returns the message as sent, merged with what the RPC returns (``id``, ``seq``,
        ``status``, ``previous_status``, ``ref``, ``created_at``).
        """
        result = await self._scalar(
            "agent_message_append",
            "select public.agent_message_append(cast(:conversation_id as uuid), "
            "cast(cast(:message as text) as jsonb))",
            {"conversation_id": conversation_id, "message": _json_param(message)},
        )
        if not isinstance(result, dict) or "id" not in result:
            raise KnowledgeRejectedError("agent_message_append returned no message id")
        return {"conversation_id": conversation_id, **message, **result}

    async def close_conversation(self, conversation_id: str, outcome: dict[str, Any]) -> dict[str, Any]:
        """Closes (or re-closes) a conversation: upserts ``deal_outcomes`` from the outcome's
        deal keys, writes the episode memory and refreshes the conversation chunk."""
        result = await self._scalar(
            "agent_conversation_close",
            "select public.agent_conversation_close(cast(:conversation_id as uuid), "
            "cast(cast(:outcome as text) as jsonb))",
            {"conversation_id": conversation_id, "outcome": _json_param(outcome)},
        )
        return result if isinstance(result, dict) else {}

    async def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        view = await self._scalar(
            "agent_conversations read",
            "select jsonb_build_object("
            "'conversation', to_jsonb(c), "
            "'messages', coalesce((select jsonb_agg(to_jsonb(m) order by m.seq) "
            "from public.agent_messages m where m.conversation_id = c.id), '[]'::jsonb), "
            "'tool_calls', coalesce((select jsonb_agg(to_jsonb(t) order by t.id) "
            "from public.agent_tool_calls t where t.conversation_id = c.id), '[]'::jsonb), "
            "'outcome', (select to_jsonb(o) from public.deal_outcomes o where o.conversation_id = c.id)) "
            "from public.agent_conversations c where c.id = cast(:id as uuid)",
            {"id": conversation_id},
        )
        return view if isinstance(view, dict) else None

    async def log_tool_call(
        self,
        *,
        conversation_id: str | None,
        message_id: str | None,
        tool: str,
        args: dict[str, Any],
        result_refs: Sequence[str],
        latency_ms: int,
    ) -> None:
        await self._scalar(
            "agent_tool_calls insert",
            "insert into public.agent_tool_calls "
            "(conversation_id, message_id, tool, args, result_refs, latency_ms) values ("
            "cast(:conversation_id as uuid), cast(:message_id as uuid), cast(:tool as text), "
            "cast(cast(:args as text) as jsonb), cast(:result_refs as text[]), "
            "cast(:latency_ms as integer)) returning id",
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "tool": tool,
                "args": _json_param(args),
                "result_refs": list(result_refs),
                "latency_ms": latency_ms,
            },
        )

    # ------------------------------------------------------------------ policy inputs (read only)

    async def active_mandate(self, company_ids: Sequence[str]) -> dict[str, Any] | None:
        """The active signed mandate (``agent_mandates``, K11) for the first matching id, if any."""
        if not company_ids:
            return None
        try:
            mandate = await self._scalar(
                "agent_mandates read",
                "select to_jsonb(m) - 'id' - 'created_at' from public.agent_mandates m "
                "where m.active and m.company_id = any(cast(:ids as text[])) "
                "order by array_position(cast(:ids as text[]), m.company_id) limit 1",
                {"ids": list(company_ids)},
            )
        except KnowledgeUnavailableError:
            return None  # table not created yet: default platform policy applies
        return mandate if isinstance(mandate, dict) else None

    async def counterparty_facts(self, publication_number: str, supplier_id: str) -> dict[str, Any]:
        """KYC/DD from ``supplier_overview`` and the latest ``deal_checks`` decision (if deployed)."""
        facts = await self._scalar(
            "supplier_overview read",
            "select jsonb_build_object("
            "'overview', (select jsonb_build_object('supplier_id', s.id, 'name', s.name, "
            "'country', s.country, 'total_score', s.total_score, 'verdict', s.verdict, "
            "'confidence', s.confidence, 'kyc_status', s.kyc_status, 'red_flag_count', s.red_flag_count) "
            "from public.supplier_overview s where s.id = cast(:supplier_id as text)), "
            "'tender', (select jsonb_build_object('publication_number', o.publication_number, "
            "'buyer_name', o.buyer_name, 'buyer_country', o.buyer_country, 'cpv_codes', o.cpv_codes, "
            "'estimated_value', o.estimated_value, 'estimated_currency', o.estimated_currency, "
            "'next_deadline', o.next_deadline, 'ted_url', o.ted_url) "
            "from public.ted_open_tenders o where o.publication_number = cast(:pub as text)))",
            {"supplier_id": supplier_id, "pub": publication_number},
        )
        facts = facts if isinstance(facts, dict) else {}
        try:
            deal_check = await self._scalar(
                "deal_checks read",
                "select jsonb_build_object('deal_id', d.deal_id, 'decision', d.decision, "
                "'blocking', d.blocking, 'conditions', d.conditions, 'checked_at', d.checked_at, "
                "'method_version', d.method_version) "
                "from public.deal_checks d where d.publication_number = cast(:pub as text) "
                "and d.supplier_id = cast(:supplier_id as text) "
                "order by d.updated_at desc limit 1",
                {"supplier_id": supplier_id, "pub": publication_number},
            )
        except KnowledgeUnavailableError:
            deal_check = None  # deal_due_diligence.py --push has not created the table yet
        facts["deal_check"] = deal_check if isinstance(deal_check, dict) else None
        return facts

    async def pick_live_tender(
        self,
        publication_number: str | None = None,
        supplier_id: str | None = None,
    ) -> dict[str, Any] | None:
        """An open TED tender with an assigned family and a profiled supplier match (demo use)."""
        return await self._scalar(
            "open tender lookup",
            "select to_jsonb(x) from ("
            "select t.publication_number, t.buyer_name, t.buyer_country, t.estimated_value, "
            "t.estimated_currency, t.next_deadline, m.supplier_id, "
            "(select i.buyer_id from public.product_items i where i.source_table = 'ted_notices' "
            "and i.source_pk = t.publication_number and i.buyer_id is not null limit 1) as buyer_id "
            "from public.ted_open_tenders t "
            "cross join lateral (select u.supplier_id from public.ui_match_tender(t.publication_number, 10) u "
            "where u.supplier_id is not null "
            "and (cast(:supplier_id as text) is null or u.supplier_id = cast(:supplier_id as text)) "
            "limit 1) m "
            "where t.next_deadline > now() "
            "and (cast(:pub as text) is null or t.publication_number = cast(:pub as text)) "
            "and exists (select 1 from public.ui_tender_families f "
            "where f.publication_number = t.publication_number and f.family_id is not null) "
            "order by (t.estimated_currency = 'EUR') desc nulls last, t.next_deadline "
            "limit 1) x",
            {"pub": publication_number, "supplier_id": supplier_id},
        )
"""Knowledge client contract (docs/agent-context-rag-implementation.md §3).

Agents and the orchestrator reach the knowledge layer only through `KnowledgeClient`.
`SimulatedKnowledgeClient` implements it on local files for the demo. A Supabase client
calling the `agent_context` RPC and writing `agent_conversations` / `agent_messages`
can replace it without changing the callers.
"""

from __future__ import annotations

from typing import Any, Protocol

# agent_conversations.status and agent_messages.type, as in §3.5.
# awaiting_review: the supplier agent has written; the buyer reviews due diligence and the deal check first.
CONVERSATION_STATUSES = ("open", "awaiting_review", "quoted", "accepted", "rejected", "escalated", "closed")
MESSAGE_TYPES = (
    "introduction", "rfq", "quote", "counter", "accept", "reject", "question",
    "answer", "reference_request", "reference_answer", "escalate", "check",
)


class KnowledgeClient(Protocol):
    async def context(
        self,
        role: str,
        purpose: str,
        subject: dict[str, Any],
        query: str | None = None,
        viewer_id: str | None = None,
        limit: int = 8,
    ) -> dict[str, Any]:
        """The context pack of §3.4 (same shape for every purpose)."""

    async def create_conversation(self, conversation: dict[str, Any]) -> dict[str, Any]: ...

    async def append_message(self, conversation_id: str, message: dict[str, Any]) -> dict[str, Any]: ...

    async def update_conversation(self, conversation_id: str, fields: dict[str, Any]) -> dict[str, Any]: ...

    async def close_conversation(self, conversation_id: str, status: str, outcome: dict[str, Any]) -> dict[str, Any]: ...

    async def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        """The conversation with its messages, or None."""

    async def list_conversations(self, company_id: str | None = None) -> list[dict[str, Any]]:
        """Conversations (with messages) where the company is the buyer or the supplier."""

    async def record_assessment(self, assessment: dict[str, Any]) -> dict[str, Any]: ...

    async def list_assessments(self, company_id: str | None = None) -> list[dict[str, Any]]: ...
