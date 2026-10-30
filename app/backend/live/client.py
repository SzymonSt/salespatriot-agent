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
