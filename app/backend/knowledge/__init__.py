"""Agent context, RAG, memory and persisted conversations (docs/agent-context-rag-implementation.md, WS5)."""
"""Knowledge layer for company agents: context packs, conversations and (later) RAG and memory.

See docs/agent-context-rag-implementation.md. Today the data comes from a simulated database
(`SimulatedKnowledgeClient`); a Supabase client implementing `KnowledgeClient` will replace it.
"""

from app.backend.knowledge.client import KnowledgeClient
from app.backend.knowledge.conversations import ConversationOrchestrator
from app.backend.knowledge.simulated import SimulatedKnowledgeClient

__all__ = ["ConversationOrchestrator", "KnowledgeClient", "SimulatedKnowledgeClient"]
