"""HTTP routes for company agents and their conversations (§3.6, simulated database).

Local and unauthenticated like the other routes: keep the server on 127.0.0.1.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.backend.live.conversations import tender_from_notice

# /v1/live: the demo flow on the simulated database. /v1/conversations etc. belong to the WS5 knowledge routes.
router = APIRouter(prefix="/v1/live")


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
