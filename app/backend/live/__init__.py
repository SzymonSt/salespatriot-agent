"""Live demo flow: company agents, their conversations and a simulated knowledge database.

See docs/agent-context-rag-implementation.md. Today the data comes from a simulated database
(`SimulatedKnowledgeClient`); a Supabase client implementing `KnowledgeClient` will replace it.
"""

from app.backend.live.client import KnowledgeClient
from app.backend.live.conversations import ConversationOrchestrator
from app.backend.live.simulated import SimulatedKnowledgeClient

__all__ = ["ConversationOrchestrator", "KnowledgeClient", "SimulatedKnowledgeClient"]
