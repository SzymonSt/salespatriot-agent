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
"""HTTP routes for company agents and their conversations (§3.6, simulated database).

Local and unauthenticated like the other routes: keep the server on 127.0.0.1.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.backend.knowledge.conversations import tender_from_notice

router = APIRouter(prefix="/v1")


class OfferBroadcast(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: dict[str, Any]
    supplier_ids: list[str] = Field(min_length=1, max_length=10)


class StartConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    buyer_id: str
    supplier_id: str
    publication_number: str


class HumanMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender_role: Literal["buyer", "supplier"]
    sender_id: str
    text: str = Field(min_length=1, max_length=2000)


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["buyer", "supplier"]
    purpose: Literal["assess_tender", "find_suppliers", "evaluate_quote", "check_counterparty", "search"]
    subject: dict[str, Any]
    query: str | None = None
    viewer_id: str


def _agents(request: Request):
    if request.app.state.settings.google_api_key is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GOOGLE_API_KEY is not configured")
    return request.app.state.orchestrator


@router.post("/agents/offers", status_code=status.HTTP_202_ACCEPTED)
async def broadcast_offer(body: OfferBroadcast, request: Request) -> dict[str, Any]:
    """A buyer posted a requirement: each listed supplier's agent assesses it in the background."""
    orchestrator = _agents(request)
    if not body.request.get("id") or not body.request.get("buyer_id") or not body.request.get("title"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "request needs id, buyer_id and title")
    known = [s for s in body.supplier_ids if s in orchestrator.profiles]
    for supplier_id in known:
        orchestrator.spawn(orchestrator.handle_request(body.request, supplier_id))
    return {"scheduled": known, "unknown": sorted(set(body.supplier_ids) - set(known))}


@router.post("/conversations", status_code=status.HTTP_202_ACCEPTED)
async def start_conversation(body: StartConversation, request: Request) -> dict[str, Any]:
    """Let a supplier's agent assess an open TED tender of this buyer and, if it fits, start talking."""
    orchestrator = _agents(request)
    buyer = orchestrator.profiles.get(body.buyer_id)
    if buyer is None or body.supplier_id not in orchestrator.profiles:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown buyer_id or supplier_id")
    notice = next((n for n in buyer.get("recent_notices") or [] if n.get("notice_number") == body.publication_number), None)
    if notice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"{body.publication_number} is not among this buyer's tenders")
    orchestrator.spawn(orchestrator.handle_tender(tender_from_notice(notice, buyer), body.supplier_id))
    return {"status": "assessing"}


@router.get("/conversations")
async def list_conversations(request: Request, company_id: str | None = None) -> list[dict[str, Any]]:
    return await request.app.state.knowledge.list_conversations(company_id)


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    conversation = await request.app.state.knowledge.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    return conversation


@router.post("/conversations/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
async def post_message(conversation_id: str, body: HumanMessage, request: Request) -> dict[str, Any]:
    """A person on one side writes; the other side's agent answers in the background."""
    orchestrator = _agents(request)
    conversation = await request.app.state.knowledge.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found")
    if conversation.get(f"{body.sender_role}_id") != body.sender_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "sender is not this side of the conversation")
    return await orchestrator.human_message(conversation_id, body.sender_role, body.sender_id, body.text)


@router.post("/conversations/{conversation_id}/resume")
async def resume_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
    """Buyer reviewed due diligence and the deal check: let its agent negotiate."""
    try:
        return await _agents(request).resume(conversation_id)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found") from None


class Approval(BaseModel):
    decision: Literal["approved", "rejected"]
    approver: str = "buyer"


@router.post("/conversations/{conversation_id}/approval")
async def approve_deal(conversation_id: str, body: Approval, request: Request) -> dict[str, Any]:
    """The human step at the end: the buyer approves or rejects the deal its agent agreed."""
    try:
        return await _agents(request).approve(conversation_id, body.decision, body.approver)
    except KeyError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation not found") from None
    except ValueError as error:
        raise HTTPException(status.HTTP_409_CONFLICT, str(error)) from None


@router.get("/agents/assessments")
async def list_assessments(request: Request, company_id: str | None = None) -> list[dict[str, Any]]:
    return await request.app.state.knowledge.list_assessments(company_id)


@router.post("/context")
async def context(body: ContextRequest, request: Request) -> dict[str, Any]:
    return await request.app.state.knowledge.context(body.role, body.purpose, body.subject, body.query, body.viewer_id)
