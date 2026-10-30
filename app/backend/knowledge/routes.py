"""HTTP routes for context, RAG, memory and persisted conversations (contract §3.6).

Local and unauthenticated like the other routes: keep the server bound to 127.0.0.1 until
organization authentication exists. ``viewer_id`` is caller-asserted here; the ADK tools bind it
from the agent factory instead.
"""

from collections.abc import Awaitable
from typing import Annotated, Any, TypeVar
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status

from app.backend.agents.runtime import configure_gemini
from app.backend.knowledge.client import (
    KnowledgeClient,
    KnowledgeError,
    KnowledgeForbiddenError,
    KnowledgeInvalidError,
    KnowledgeNotFoundError,
    KnowledgeRejectedError,
    KnowledgeUnavailableError,
)
from app.backend.knowledge.conversations import (
    ConversationAgentError,
    ConversationError,
    ConversationForbiddenError,
    ConversationNotFoundError,
    ConversationOrchestrator,
    ConversationPolicyError,
)
from app.backend.knowledge.embeddings import QueryEmbedder
from app.backend.knowledge.models import (
    ContextRequest,
    CreateConversationRequest,
    KnowledgeSearchRequest,
    MemoryFeedbackRequest,
    MemoryRecallRequest,
    MemoryWriteRequest,
    OutcomeRequest,
    PostMessageRequest,
    RunLiveDemoRequest,
)

router = APIRouter()
T = TypeVar("T")

AGENT_TURN_MESSAGES = {("buyer", "rfq"), ("buyer", "counter"), ("supplier", "quote")}


def get_knowledge_client(request: Request) -> KnowledgeClient:
    client = getattr(request.app.state, "knowledge_client", None)
    if client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DATABASE_URL is unset or still contains a placeholder",
        )
    return client


def get_query_embedder(request: Request) -> QueryEmbedder | None:
    return getattr(request.app.state, "query_embedder", None)


def get_orchestrator(
    request: Request,
    client: Annotated[KnowledgeClient, Depends(get_knowledge_client)],
    embedder: Annotated[QueryEmbedder | None, Depends(get_query_embedder)],
) -> ConversationOrchestrator:
    settings = request.app.state.settings
    return ConversationOrchestrator(
        client=client,
        embedder=embedder,
        model=settings.gemini_model,
        profiles=getattr(request.app.state, "profiles", None),
    )


def _require_gemini(request: Request) -> None:
    settings = request.app.state.settings
    if settings.google_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GOOGLE_API_KEY is not configured",
        )
    configure_gemini(settings)


def _http_error(error: Exception) -> HTTPException:
    if isinstance(error, KnowledgeUnavailableError):
        code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif isinstance(error, (KnowledgeRejectedError, ConversationPolicyError)):
        code = status.HTTP_409_CONFLICT
    elif isinstance(error, KnowledgeInvalidError):
        code = 422
    elif isinstance(error, (KnowledgeNotFoundError, ConversationNotFoundError)):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, (KnowledgeForbiddenError, ConversationForbiddenError)):
        code = status.HTTP_403_FORBIDDEN
    elif isinstance(error, ConversationAgentError):
        detail = str(error)
        if error.conversation_id:
            detail = f"{detail} (conversation {error.conversation_id} was escalated)"
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
    else:
        code = status.HTTP_500_INTERNAL_SERVER_ERROR
    return HTTPException(status_code=code, detail=str(error))


async def _guard(call: Awaitable[T]) -> T:
    try:
        return await call
    except (KnowledgeError, ConversationError) as error:
        raise _http_error(error) from error


async def _embed(embedder: QueryEmbedder | None, query: str | None, response: Response) -> list[float] | None:
    if not query:
        response.headers["X-Query-Embedding"] = "none"
        return None
    vector = await embedder.embed(query) if embedder is not None else None
    response.headers["X-Query-Embedding"] = "used" if vector is not None else "unavailable"
    return vector


# --------------------------------------------------------------------------- context and RAG


@router.post("/v1/context")
async def get_context(
    body: ContextRequest,
    response: Response,
    client: Annotated[KnowledgeClient, Depends(get_knowledge_client)],
    embedder: Annotated[QueryEmbedder | None, Depends(get_query_embedder)],
) -> dict[str, Any]:
    vector = await _embed(embedder, body.query, response)
    return await _guard(
        client.context(
            role=body.role,
            purpose=body.purpose,
            subject=body.subject,
            query_embedding=vector,
            query_text=body.query,
            viewer=body.viewer_id,
            limit=body.limit,
        )
    )


@router.post("/v1/knowledge/search")
async def search_knowledge(
    body: KnowledgeSearchRequest,
    response: Response,
    client: Annotated[KnowledgeClient, Depends(get_knowledge_client)],
    embedder: Annotated[QueryEmbedder | None, Depends(get_query_embedder)],
) -> dict[str, Any]:
    vector = await _embed(embedder, body.query, response)
    hits = await _guard(
        client.search(
            query_embedding=vector,
            query_text=body.query,
            kinds=body.kinds,
            family_id=body.family_id,
            calibre=body.calibre,
            cpv_prefix=body.cpv_prefix,
            country=body.country,
            viewer=body.viewer_id,
            limit=body.limit,
        )
    )
    return {"query": body.query, "hits": hits}


# --------------------------------------------------------------------------- memory


@router.post("/v1/memory/recall")
async def recall_memory(
    body: MemoryRecallRequest,
    response: Response,
    client: Annotated[KnowledgeClient, Depends(get_knowledge_client)],
    embedder: Annotated[QueryEmbedder | None, Depends(get_query_embedder)],
) -> dict[str, Any]:
    vector = await _embed(embedder, body.query, response)
    memories = await _guard(
        client.recall(
            query_embedding=vector,
            role=body.role,
            family_id=body.family_id,
            country=body.country,
            counterparty=body.counterparty,
            viewer=body.viewer_id,
            limit=body.limit,
        )
    )
    return {"memories": memories}


@router.post("/v1/memory", status_code=status.HTTP_201_CREATED)
async def write_memory(
    body: MemoryWriteRequest,
    client: Annotated[KnowledgeClient, Depends(get_knowledge_client)],
) -> dict[str, str]:
    memory_id = await _guard(client.remember(body.to_entry()))
    return {"id": memory_id, "ref": f"memory:{memory_id}", "status": body.status}


@router.post("/v1/memory/{memory_id}/feedback")
async def memory_feedback(
    memory_id: UUID,
    body: MemoryFeedbackRequest,
    client: Annotated[KnowledgeClient, Depends(get_knowledge_client)],
) -> dict[str, Any]:
    counters = await _guard(client.memory_feedback(str(memory_id), body.helpful))
    if counters is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Memory entry was not found")
    return counters


# --------------------------------------------------------------------------- conversations


@router.post("/v1/conversations", status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: CreateConversationRequest,
    request: Request,
    orchestrator: Annotated[ConversationOrchestrator, Depends(get_orchestrator)],
) -> dict[str, Any]:
    _require_gemini(request)
    return await _guard(orchestrator.start(body))


@router.post("/v1/conversations/{conversation_id}/messages")
async def post_message(
    conversation_id: UUID,
    body: PostMessageRequest,
    request: Request,
    orchestrator: Annotated[ConversationOrchestrator, Depends(get_orchestrator)],
) -> dict[str, Any]:
    if (body.sender_role, body.type) in AGENT_TURN_MESSAGES:
        _require_gemini(request)
    return await _guard(orchestrator.post_message(str(conversation_id), body))


@router.get("/v1/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: UUID,
    client: Annotated[KnowledgeClient, Depends(get_knowledge_client)],
) -> dict[str, Any]:
    view = await _guard(client.get_conversation(str(conversation_id)))
    if view is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation was not found")
    return view


@router.post("/v1/conversations/{conversation_id}/outcome")
async def record_outcome(
    conversation_id: UUID,
    body: OutcomeRequest,
    orchestrator: Annotated[ConversationOrchestrator, Depends(get_orchestrator)],
) -> dict[str, Any]:
    return await _guard(orchestrator.record_outcome(str(conversation_id), body))


# --------------------------------------------------------------------------- persisted demo


@router.post("/api/demo/run-live")
async def run_live_demo(
    request: Request,
    orchestrator: Annotated[ConversationOrchestrator, Depends(get_orchestrator)],
    body: Annotated[RunLiveDemoRequest | None, Body()] = None,
) -> dict[str, Any]:
    _require_gemini(request)
    return await _guard(orchestrator.run_live_demo(body or RunLiveDemoRequest()))
