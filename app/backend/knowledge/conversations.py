"""Persisted buyer ⇄ supplier conversations (plan §5 WS5 5.4).

Flow of a full run:

1. supplier ``assess_tender`` → ``introduction`` (non-binding);
2. buyer ``find_suppliers`` → policy gate in Python (mandate + KYC/DD + deal check) → ``check``,
   then ``rfq`` (or ``escalate`` / ``reject``);
3. supplier ``assess_tender`` → structured indicative ``quote``, checked against the supplier mandate;
4. buyer ``evaluate_quote`` → recommendation from the model, decision in Python against the buyer
   mandate (autonomous value limit, price ceiling, max rounds, minimum cited evidence) →
   ``accept``, ``counter`` (back to 3) or ``escalate``;
5. on ``accept``: ``agent_conversation_close`` with the outcome.

Every message goes through ``agent_message_append`` (sequence + state machine in the database), and
every context fetch and agent tool call is logged in ``agent_tool_calls``. The backend fetches the
context pack for each step itself (the policy gates need it) and passes it to the agent in the
payload; the agent also has the knowledge tools for follow-up lookups.
"""

import logging
import math
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from google.adk.agents import Agent
from pydantic import BaseModel, ValidationError

from app.backend.agents.main import (
    BuyerMessageDraft,
    BuyerQuoteEvaluation,
    SupplierIntroductionDraft,
    SupplierQuoteDraft,
    build_buyer_agent,
    build_buyer_evaluation_agent,
    build_supplier_agent,
    build_supplier_quote_agent,
)
from app.backend.agents.runtime import AgentRuntimeError, run_structured_agent
from app.backend.knowledge.client import KnowledgeClient, KnowledgeError
from app.backend.knowledge.embeddings import QueryEmbedder
from app.backend.knowledge.models import (
    CreateConversationRequest,
    Mandate,
    OutcomeRequest,
    PostMessageRequest,
    RunLiveDemoRequest,
)
from app.backend.knowledge.tools import (
    KnowledgeToolContext,
    ToolCallRecorder,
    build_knowledge_tools,
    collect_refs,
)
from app.backend.profiles import ProfileRegistry

logger = logging.getLogger(__name__)

AGENT_VERSION = "ws5-v1"
DEFAULT_MANDATE_SOURCE = "default platform policy (no signed mandate yet)"
POLICY_SENDER_ID = "platform:policy-gate"
MAX_NEGOTIATION_TURNS = 6
DEMO_DISCLAIMER = (
    "Demo run on a real open TED tender. The buyer agent acts on behalf of the contracting "
    "authority as a simulation; no message is sent to the buyer or the supplier, no bid is "
    "submitted, and quotes are indicative and non-binding. Mandates without a signed "
    "agent_mandates row use the default platform policy."
)

OutputModel = TypeVar("OutputModel", bound=BaseModel)
AgentRunner = Callable[..., Awaitable[BaseModel]]


class ConversationError(RuntimeError):
    """Base class; ``str(error)`` is safe to return to API callers."""


class ConversationNotFoundError(ConversationError):
    pass


class ConversationForbiddenError(ConversationError):
    pass


class ConversationPolicyError(ConversationError):
    pass


class ConversationAgentError(ConversationError):
    def __init__(self, message: str, conversation_id: str | None) -> None:
        super().__init__(message)
        self.conversation_id = conversation_id


# --------------------------------------------------------------------------- pure policy helpers


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass
class GateResult:
    decision: str  # CLEARED_TO_REQUEST_QUOTE | ESCALATE | NO_GO
    checks: list[dict[str, str]]
    conditions: list[str]

    @property
    def blocking(self) -> list[str]:
        return [check["summary"] for check in self.checks if check["status"] in {"escalate", "fail"}]


def buyer_gate(
    *,
    supplier_id: str,
    mandate: Mandate,
    pack: dict[str, Any],
    facts: dict[str, Any],
) -> GateResult:
    """Buyer policy gate before an RFQ: mandate (K11 / D14), KYC, DD, deal check, evidence."""
    checks: list[dict[str, str]] = []
    conditions: list[str] = []

    def add(code: str, status: str, summary: str) -> None:
        checks.append({"code": code, "status": status, "summary": summary})

    candidates = pack.get("candidates") or []
    if candidates:
        if any(isinstance(c, dict) and c.get("supplier_id") == supplier_id for c in candidates):
            add("match", "pass", "Supplier is among the matched candidates for this tender")
        else:
            add("match", "escalate", "Supplier is not among the matched candidates for this tender")
    else:
        add("match", "info", "Candidate list unavailable; match not confirmed by find_suppliers")

    overview = facts.get("overview") or {}
    score = _number(overview.get("total_score"))
    if score is None:
        add("dd_score", "escalate", "Supplier due-diligence score is unknown")
    elif score < mandate.min_supplier_score:
        add("dd_score", "escalate", f"Supplier score {score:g} < mandate minimum {mandate.min_supplier_score:g}")
    else:
        add("dd_score", "pass", f"Supplier score {score:g} ≥ {mandate.min_supplier_score:g}")

    kyc = overview.get("kyc_status")
    if not kyc:
        add("kyc", "escalate", "Supplier KYC status is unknown")
    elif kyc not in mandate.allowed_kyc_status:
        add("kyc", "escalate", f"Supplier KYC status {kyc!r} is not allowed by the mandate")
    else:
        add("kyc", "pass", f"Supplier KYC status {kyc!r} allowed")

    tender = facts.get("tender") or (pack.get("facts") or {}).get("tender") or {}
    cpv_codes = [str(code) for code in tender.get("cpv_codes") or []]
    if not cpv_codes:
        add("category", "escalate", "Tender CPV codes are unknown")
    elif any(code.startswith(tuple(mandate.allowed_cpv_prefixes)) for code in cpv_codes):
        add("category", "pass", "Tender category inside the mandate")
    else:
        add("category", "escalate", "Tender category outside the mandate")

    value = _number(tender.get("estimated_value"))
    currency = tender.get("estimated_currency") or tender.get("currency")
    if value is None:
        add("value", "condition", "Estimated value not published; the quote is checked at acceptance")
        conditions.append(
            f"Accept only an offer ≤ €{mandate.max_autonomous_value_eur:,.0f}; above that a human decides"
        )
    elif currency != "EUR":
        add("value", "condition", f"Estimated value in {currency}; checked against the EUR limit at acceptance")
    elif value > mandate.max_autonomous_value_eur:
        add("value", "escalate",
            f"Estimated value €{value:,.0f} > autonomous limit €{mandate.max_autonomous_value_eur:,.0f}")
    else:
        add("value", "pass", f"Estimated value €{value:,.0f} within the autonomous limit")

    deal_check = facts.get("deal_check") or {}
    decision = deal_check.get("decision")
    if decision == "NO-GO":
        add("deal_check", "fail", "Deal due diligence (D1–D14): NO-GO")
    elif decision == "ESCALATE":
        add("deal_check", "escalate", "Deal due diligence (D1–D14): ESCALATE")
    elif decision:
        add("deal_check", "pass", f"Deal due diligence (D1–D14): {decision}")
        conditions.extend(str(c) for c in deal_check.get("conditions") or [])
    else:
        add("deal_check", "info", "No deal due-diligence run stored for this tender and supplier")

    evidence_refs = collect_refs(pack.get("evidence") or [])
    if len(evidence_refs) < mandate.min_evidence_refs:
        add("evidence", "escalate",
            f"{len(evidence_refs)} evidence item(s) < mandate minimum {mandate.min_evidence_refs}")
    else:
        add("evidence", "pass", f"{len(evidence_refs)} evidence item(s) with refs")

    if mandate.source == DEFAULT_MANDATE_SOURCE:
        conditions.append("Both parties sign their agent mandate (K11)")

    statuses = {check["status"] for check in checks}
    if "fail" in statuses:
        result = "NO_GO"
    elif "escalate" in statuses:
        result = "ESCALATE"
    else:
        result = "CLEARED_TO_REQUEST_QUOTE"
    return GateResult(decision=result, checks=checks, conditions=conditions)


@dataclass
class QuoteCheck:
    quote: dict[str, Any] | None
    problems: list[str]


def check_supplier_quote(draft: SupplierQuoteDraft, mandate: Mandate) -> QuoteCheck:
    """Validates the model's numbers and the supplier mandate. Returns the quote payload."""
    unit_price = _number(draft.unit_price)
    quantity = _number(draft.quantity)
    currency = (draft.currency or "").strip().upper()
    problems: list[str] = []
    if unit_price is None or unit_price <= 0:
        problems.append("unit price must be a positive number")
    if quantity is None or quantity <= 0:
        problems.append("quantity must be a positive number")
    if not re.fullmatch(r"[A-Z]{3}", currency):
        problems.append("currency must be an ISO 4217 code")
    if draft.lead_time_days < 0:
        problems.append("lead time must not be negative")
    if problems:
        return QuoteCheck(quote=None, problems=[f"Invalid quote: {p}" for p in problems])

    assert unit_price is not None and quantity is not None
    total = round(unit_price * quantity, 2)
    if mandate.min_unit_price is not None:
        if mandate.currency != currency:
            problems.append(f"Quote in {currency} cannot be checked against the floor in {mandate.currency}")
        elif unit_price < mandate.min_unit_price:
            problems.append(f"Unit price {unit_price:g} {currency} < supplier floor {mandate.min_unit_price:g}")
    if currency != "EUR":
        problems.append(f"Total {total:,.2f} {currency} cannot be checked against the EUR autonomous limit")
    elif total > mandate.max_autonomous_value_eur:
        problems.append(f"Total €{total:,.2f} > supplier autonomous limit €{mandate.max_autonomous_value_eur:,.0f}")

    quote = {
        "unit_price": unit_price,
        "currency": currency,
        "quantity": quantity,
        "quantity_unit": draft.quantity_unit,
        "quantity_is_indicative": draft.quantity_is_indicative,
        "total_price": total,
        "lead_time_days": draft.lead_time_days,
        "incoterms": draft.incoterms,
        "validity_days": draft.validity_days,
        "conditions": draft.conditions,
        "caveats": draft.caveats,
        "indicative": True,
        "binding": False,
    }
    return QuoteCheck(quote=quote, problems=problems)


@dataclass
class BuyerDecision:
    action: str  # accept | counter | escalate
    reasons: list[str]
    counter_unit_price: float | None = None
    overridden: bool = False


def decide_on_quote(
    *,
    evaluation: BuyerQuoteEvaluation,
    quote: dict[str, Any],
    mandate: Mandate,
    rounds_used: int,
    cited_refs: list[str],
) -> BuyerDecision:
    """The buyer's decision. The model recommends; the mandate decides."""
    unit_price = float(quote["unit_price"])
    quantity = float(quote["quantity"])
    currency = quote["currency"]
    total = round(unit_price * quantity, 2)

    hard: list[str] = []
    if currency != "EUR":
        hard.append(f"Total {total:,.2f} {currency} cannot be checked against the EUR autonomous limit")
    elif total > mandate.max_autonomous_value_eur:
        hard.append(f"Total €{total:,.2f} > buyer autonomous limit €{mandate.max_autonomous_value_eur:,.0f}")
    over_ceiling = False
    if mandate.price_ceiling is not None:
        if mandate.currency != currency:
            hard.append(f"Quote in {currency} cannot be checked against the price ceiling in {mandate.currency}")
        elif total > mandate.price_ceiling:
            over_ceiling = True
    if len(cited_refs) < mandate.min_evidence_refs:
        hard.append(f"Evaluation cites {len(cited_refs)} ref(s) < mandate minimum {mandate.min_evidence_refs}")

    if hard:
        return BuyerDecision("escalate", hard, overridden=evaluation.recommendation != "escalate")
    if evaluation.recommendation == "escalate":
        return BuyerDecision("escalate", evaluation.rationale or ["Buyer agent recommended escalation"])
    if evaluation.recommendation == "accept" and not over_ceiling:
        return BuyerDecision("accept", evaluation.rationale or ["Within the buyer mandate"])

    if rounds_used >= mandate.max_rounds:
        return BuyerDecision(
            "escalate",
            [f"Negotiation rounds exhausted ({rounds_used} of {mandate.max_rounds})"],
            overridden=True,
        )
    counter = _number(evaluation.counter_unit_price)
    overridden = evaluation.recommendation != "counter"
    if over_ceiling:
        cap = round(float(mandate.price_ceiling or 0) / quantity, 4)
        if counter is None or counter > cap:
            counter, overridden = cap, True
    if counter is None or counter <= 0 or counter >= unit_price:
        return BuyerDecision("escalate", ["No valid counter price below the quote"], overridden=True)
    reasons = evaluation.rationale or []
    if over_ceiling:
        reasons = [*reasons, f"Total above the price ceiling {mandate.price_ceiling:,.2f} {currency}"]
    return BuyerDecision("counter", reasons, counter_unit_price=counter, overridden=overridden)


def filter_refs(cited: list[str], allowed: set[str]) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    dropped: list[str] = []
    for ref in cited:
        ref = ref.strip()
        if not ref:
            continue
        (kept if ref in allowed else dropped).append(ref)
    return list(dict.fromkeys(kept)), list(dict.fromkeys(dropped))


# --------------------------------------------------------------------------- orchestration


@dataclass
class DealState:
    conversation_id: str
    publication_number: str
    buyer_id: str
    supplier_id: str
    buyer_mandate: Mandate
    supplier_mandate: Mandate
    demo: bool
    facts: dict[str, Any] | None = None
    rounds: int = 0
    last_quote: dict[str, Any] | None = None
    last_buyer_message: dict[str, Any] | None = None
    pending_counter: dict[str, Any] | None = None  # the latest offer, when it is a buyer counter
    status: str = "open"
    steps: list[dict[str, Any]] = field(default_factory=list)
"""Agent-to-agent conversation orchestrator (docs/agent-context-rag-implementation.md §5.4).

Flow for a new buyer request:
1. the supplier's agent assesses whether its company could ever supply it (SupplierAssessment);
2. if yes, a conversation is created and the supplier agent writes to the buyer agent;
3. the two company agents take turns until accept, reject or escalate.

Mandate rules run here in Python, never in the model: a buyer accept needs a quote within the
budget and no open human-approval reason; counters are limited; a supplier quote below its price
floor escalates. Every turn is persisted through the KnowledgeClient, so the UI can poll it.
Turns use the two-step path of §5.2: the backend fetches the context pack and the agent only drafts.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from pydantic import BaseModel

from app.backend.agents.company import (
    BUYER_TURN_TYPES,
    SUPPLIER_TURN_TYPES,
    AgentTurn,
    SupplierAssessment,
    build_company_agent,
)
from app.backend.agents.runtime import AgentRuntimeError, run_structured_agent
from app.backend.due_diligence import initial_due_diligence
from app.backend.knowledge.client import KnowledgeClient
from app.backend.knowledge.context import tender_facts
from app.backend.knowledge.simulated import now

AGENT_VERSION = "company-v1"
TERMINAL = {"accepted", "rejected", "closed"}
ENDING_TYPES = {"accept", "reject", "escalate"}
MAX_COUNTERS = 2
PRICE_FLOOR = 0.97  # supplier may not quote more than 3% under its awarded TED price
# Declared supplier price lists (demo): the price basis when TED has no awarded unit price.
PRICE_LISTS = Path(__file__).resolve().parents[3] / "data" / "price_lists.json"


def price_list_entry(supplier_id: str, tender: dict[str, Any]) -> dict[str, Any] | None:
    """First price-list line of this supplier whose keywords all appear in the request title."""
    try:
        lists = json.loads(PRICE_LISTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    title = (tender.get("title") or "").lower()
    for entry in lists.get(supplier_id, []):
        if all(re.search(re.escape(k.lower()), title) for k in entry.get("keywords", [])):
            return entry
    return None

Runner = Callable[..., Awaitable[BaseModel]]


def tender_from_request(request: dict[str, Any], buyer: dict[str, Any] | None) -> dict[str, Any]:
    """A buyer requirement posted on the platform (serve.py /api/offers), as a tender record."""
    buyer = buyer or {}
    return {
        "ref": f"request:{request['id']}",
        "title": request.get("title", ""),
        "description": request.get("description", ""),
        "cpv_codes": request.get("cpv_codes") or [],
        "quantity": request.get("quantity"),
        "unit": request.get("unit") or "",
        "budget": request.get("budget"),
        "currency": request.get("currency") or "EUR",
        "need_by": request.get("need_by") or "",
        "buyer_id": request.get("buyer_id") or buyer.get("id"),
        "buyer_name": buyer.get("name"),
        "buyer_country": buyer.get("country"),
        # D1-D14 result for this supplier (serve.py runs deal_due_diligence before the agent starts).
        "deal_check": request.get("deal_check"),
    }


def tender_from_notice(notice: dict[str, Any], buyer: dict[str, Any]) -> dict[str, Any]:
    """An open TED tender from the buyer's profile, as a tender record."""
    return {
        "ref": f"ted:{notice['notice_number']}",
        "ted_url": notice.get("ted_url"),
        "title": notice.get("title", ""),
        "description": notice.get("description", ""),
        "cpv_codes": notice.get("cpv_codes") or [],
        "quantity": None,
        "unit": "",
        "budget": float(notice["value"]) if notice.get("value") else None,
        "currency": notice.get("currency") or "EUR",
        "need_by": "",
        "buyer_id": buyer["id"],
        "buyer_name": buyer.get("name"),
        "buyer_country": buyer.get("country"),
    }


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


class ConversationOrchestrator:
    def __init__(
        self,
<<<<<<< HEAD
        *,
        client: KnowledgeClient,
        embedder: QueryEmbedder | None,
        model: str,
        profiles: ProfileRegistry | None = None,
        run_agent: AgentRunner | None = None,
    ) -> None:
        self.client = client
        self.embedder = embedder
        self.model = model
        self.profiles = profiles
        self._run_agent = run_agent

    # ----------------------------------------------------------------- public entry points

    async def start(self, request: CreateConversationRequest) -> dict[str, Any]:
        facts = await self.client.counterparty_facts(request.publication_number, request.supplier_id)
        tender = facts.get("tender") or {}
        if not tender:
            raise ConversationNotFoundError(
                f"Tender {request.publication_number!r} is not an open TED tender"
            )
        if not request.demo:
            self._require_opt_in(request, tender)

        buyer_mandate = await self._resolve_mandate(
            request.buyer_mandate, [request.buyer_id, f"buyer:{tender.get('buyer_name')}"]
        )
        supplier_mandate = await self._resolve_mandate(request.supplier_mandate, [request.supplier_id])

        started = time.perf_counter()
        pack = await self.client.context(
            role="supplier",
            purpose="assess_tender",
            subject={"publication_number": request.publication_number, "supplier_id": request.supplier_id},
            viewer=request.supplier_id,
        )
        prefetch = self._prefetch_record("supplier", "assess_tender", pack, started,
                                         publication_number=request.publication_number,
                                         supplier_id=request.supplier_id)
        # family_id, calibre and country are filled from ui_tender_families by the insert trigger
        conversation = await self.client.create_conversation(
            publication_number=request.publication_number,
            buyer_id=request.buyer_id,
            supplier_id=request.supplier_id,
            buyer_mandate=buyer_mandate.model_dump(),
            supplier_mandate=supplier_mandate.model_dump(),
            demo=request.demo,
        )
        state = DealState(
            conversation_id=str(conversation["id"]),
            publication_number=request.publication_number,
            buyer_id=request.buyer_id,
            supplier_id=request.supplier_id,
            buyer_mandate=buyer_mandate,
            supplier_mandate=supplier_mandate,
            demo=request.demo,
            facts=facts,
        )

        async with self._agent_failure_guard(state):
            await self._supplier_introduction(state, pack, prefetch)
            if request.run == "full":
                await self._negotiate(state)
        return await self._result(state)

    async def run_live_demo(self, request: RunLiveDemoRequest) -> dict[str, Any]:
        picked = await self.client.pick_live_tender(request.publication_number, request.supplier_id)
        if not picked:
            raise ConversationNotFoundError(
                "No open TED tender with an assigned product family and a matched profiled supplier"
            )
        buyer_id = picked.get("buyer_id") or f"buyer:{picked['buyer_name']}"
        result = await self.start(
            CreateConversationRequest(
                publication_number=picked["publication_number"],
                buyer_id=buyer_id,
                supplier_id=picked["supplier_id"],
                buyer_mandate=request.buyer_mandate,
                supplier_mandate=request.supplier_mandate,
                demo=True,
                run="full",
            )
        )
        return {
            **result,
            "tender": picked,
            "buyer_is_simulated": True,
            "buyer_id_is_name_key": picked.get("buyer_id") is None,
            "disclaimer": DEMO_DISCLAIMER,
        }

    async def post_message(self, conversation_id: str, message: PostMessageRequest) -> dict[str, Any]:
        state = await self._load_state(conversation_id)
        expected = {"buyer": state.buyer_id, "supplier": state.supplier_id}.get(message.sender_role)
        if expected is not None and message.sender_id != expected:
            raise ConversationForbiddenError(
                f"sender_id does not match the conversation's {message.sender_role}"
            )
        payload = dict(message.payload)
        if message.type == "quote":
            quote = self._quote_from_payload({"payload": payload})
            if quote is None:
                raise ConversationPolicyError(
                    "A quote needs payload.unit_price, payload.currency and payload.quantity"
                )
            payload.update(quote)
        stored = await self.client.append_message(
            conversation_id,
            {
                "sender_role": message.sender_role,
                "sender_id": message.sender_id,
                "type": message.type,
                "text": message.text,
                "payload": payload,
                "refs": message.refs,
                "model": None,
                "agent_version": None,
            },
        )
        state.steps.append({"step": "external_message", "type": message.type, "message_id": stored.get("id")})

        async with self._agent_failure_guard(state):
            if message.type in {"rfq", "counter"} and message.sender_role == "buyer":
                state.last_buyer_message = stored
                if message.type == "counter":
                    state.rounds += 1
                await self._supplier_quote(state)
            elif message.type == "quote" and message.sender_role == "supplier":
                state.last_quote = stored
                await self._buyer_evaluation(state)
            elif message.type == "accept":
                terms = payload
                if self._quote_from_payload({"payload": terms}) is None and state.pending_counter:
                    terms = self._counter_terms(state.pending_counter, state.last_quote)
                await self._close_accepted(state, accepted_by=message.sender_role, terms=terms)
        return await self._result(state)

    async def record_outcome(self, conversation_id: str, outcome: OutcomeRequest) -> dict[str, Any]:
        """Upserts ``deal_outcomes``, (re)writes the episode memory and refreshes the conversation
        chunk, all inside ``agent_conversation_close``; closes the conversation if still open."""
        if await self.client.get_conversation(conversation_id) is None:
            raise ConversationNotFoundError(f"Conversation {conversation_id} was not found")
        closed = await self.client.close_conversation(conversation_id, outcome.model_dump(exclude_none=True))
        view = await self.client.get_conversation(conversation_id) or {}
        return {**view, "close": closed}

    # ----------------------------------------------------------------- steps

    async def _negotiate(self, state: DealState) -> None:
        if not await self._buyer_rfq(state):
            return
        for _ in range(MAX_NEGOTIATION_TURNS):
            if not await self._supplier_quote(state):
                return
            action = await self._buyer_evaluation(state)
            if action != "counter":
                return
        await self._append(state, "system", POLICY_SENDER_ID, "escalate",
                           "Escalated: the negotiation reached the platform turn limit.",
                           {"reasons": ["platform turn limit reached"]}, [])
        state.status = "escalated"

    async def _supplier_introduction(
        self, state: DealState, pack: dict[str, Any], prefetch: dict[str, Any]
    ) -> None:
        recorder = ToolCallRecorder()
        agent = build_supplier_agent(state.supplier_id, self.model, tools=self._tools(state, "supplier", recorder))
        draft = await self._run(agent, SupplierIntroductionDraft, state.supplier_id, {
            "demo_only": state.demo,
            "task": "Non-binding introduction to the buyer for this tender.",
            "context_pack": pack,
        })
        refs, dropped = filter_refs(draft.refs, set(collect_refs(pack)) | recorder.seen_refs)
        message = await self._append(
            state, "supplier", state.supplier_id, "introduction", draft.non_binding_message,
            {"caveats": draft.caveats, "binding": False, "demo": state.demo, "dropped_refs": dropped},
            refs, model=self.model,
        )
        await self._log_tools(state, message, [prefetch], recorder)

    async def _buyer_rfq(self, state: DealState) -> bool:
        started = time.perf_counter()
        pack = await self.client.context(
            role="buyer", purpose="find_suppliers",
            subject={"publication_number": state.publication_number}, viewer=state.buyer_id,
        )
        prefetch = self._prefetch_record("buyer", "find_suppliers", pack, started,
                                         publication_number=state.publication_number)
        facts = state.facts or await self.client.counterparty_facts(state.publication_number, state.supplier_id)
        gate = buyer_gate(supplier_id=state.supplier_id, mandate=state.buyer_mandate, pack=pack, facts=facts)
        check_refs = list(dict.fromkeys(
            collect_refs(pack.get("facts") or {})[:5] + collect_refs(pack.get("evidence") or [])[:5]
        ))
        check = await self._append(
            state, "system", POLICY_SENDER_ID, "check",
            f"Buyer policy gate: {gate.decision}" + (f" ({'; '.join(gate.blocking)})" if gate.blocking else ""),
            {"decision": gate.decision, "checks": gate.checks, "conditions": gate.conditions,
             "mandate_source": state.buyer_mandate.source, "counterparty": facts.get("overview"),
             "deal_check": facts.get("deal_check")},
            check_refs,
        )
        await self._log_tools(state, check, [prefetch], None)

        action, message_type = {
            "CLEARED_TO_REQUEST_QUOTE": ("request_quote", "rfq"),
            "ESCALATE": ("escalate", "escalate"),
            "NO_GO": ("decline", "reject"),
        }[gate.decision]
        recorder = ToolCallRecorder()
        agent = build_buyer_agent(state.buyer_id, self.model, tools=self._tools(state, "buyer", recorder))
        draft = await self._run(agent, BuyerMessageDraft, state.buyer_id, {
            "demo_only": state.demo,
            "authorized_action": action,
            "decision": gate.decision,
            "evidence_summary": "; ".join(c["summary"] for c in gate.checks),
            "conditions": gate.conditions,
            "permitted_data": ["tender title", "quantity", "delivery deadline", "product family", "calibre"],
            "context_pack": pack,
        })
        if draft.action != action:
            raise AgentRuntimeError("Agent output exceeded the buyer-authorized action")
        refs, dropped = filter_refs(draft.refs, set(collect_refs(pack)) | recorder.seen_refs)
        message = await self._append(
            state, "buyer", state.buyer_id, message_type, draft.message,
            {"gate_decision": gate.decision, "conditions": gate.conditions, "binding": False,
             "demo": state.demo, "dropped_refs": dropped, **({"reasons": gate.blocking} if gate.blocking else {})},
            refs, model=self.model,
        )
        await self._log_tools(state, message, [], recorder)
        state.last_buyer_message = message
        if message_type == "rfq":
            return True
        state.status = "escalated" if message_type == "escalate" else "rejected"
        return False

    async def _supplier_quote(self, state: DealState) -> bool:
        trigger = state.last_buyer_message or {}
        query = trigger.get("text") or None
        started = time.perf_counter()
        pack = await self.client.context(
            role="supplier", purpose="assess_tender",
            subject={"publication_number": state.publication_number, "supplier_id": state.supplier_id},
            query_embedding=await self._embed(query), query_text=query, viewer=state.supplier_id,
        )
        prefetch = self._prefetch_record("supplier", "assess_tender", pack, started,
                                         publication_number=state.publication_number,
                                         supplier_id=state.supplier_id, query=query)
        recorder = ToolCallRecorder()
        agent = build_supplier_quote_agent(state.supplier_id, self.model, tools=self._tools(state, "supplier", recorder))
        mandate = state.supplier_mandate
        draft = await self._run(agent, SupplierQuoteDraft, state.supplier_id, {
            "demo_only": state.demo,
            "task": "Indicative, non-binding quote in reply to the buyer's message.",
            "buyer_message": {"type": trigger.get("type"), "text": trigger.get("text"),
                              "payload": trigger.get("payload") or {}},
            "previous_quote": (state.last_quote or {}).get("payload"),
            "supplier_mandate": {"min_unit_price": mandate.min_unit_price, "currency": mandate.currency,
                                 "max_autonomous_value_eur": mandate.max_autonomous_value_eur},
            "context_pack": pack,
        })
        checked = check_supplier_quote(draft, mandate)
        refs, dropped = filter_refs(draft.refs, set(collect_refs(pack)) | recorder.seen_refs)
        if checked.quote is None or checked.problems:
            message = await self._append(
                state, "supplier", state.supplier_id, "escalate",
                "Escalated to a human: the quote is outside the supplier's agent mandate.",
                {"reasons": checked.problems, "draft": draft.model_dump(), "mandate_source": mandate.source,
                 "demo": state.demo},
                refs, model=self.model,
            )
            await self._log_tools(state, message, [prefetch], recorder)
            state.status = "escalated"
            return False
        message = await self._append(
            state, "supplier", state.supplier_id, "quote", draft.message,
            {**checked.quote, "round": state.rounds, "mandate_source": mandate.source,
             "demo": state.demo, "dropped_refs": dropped},
            refs, model=self.model,
        )
        await self._log_tools(state, message, [prefetch], recorder)
        state.last_quote = message
        state.status = "quoted"
        return True

    async def _buyer_evaluation(self, state: DealState) -> str:
        quote_message = state.last_quote or {}
        quote = quote_message.get("payload") or {}
        started = time.perf_counter()
        pack = await self.client.context(
            role="buyer", purpose="evaluate_quote",
            subject={"conversation_id": state.conversation_id, "message_id": str(quote_message.get("id"))},
            viewer=state.buyer_id,
        )
        prefetch = self._prefetch_record("buyer", "evaluate_quote", pack, started,
                                         conversation_id=state.conversation_id,
                                         message_id=str(quote_message.get("id")))
        recorder = ToolCallRecorder()
        agent = build_buyer_evaluation_agent(state.buyer_id, self.model, tools=self._tools(state, "buyer", recorder))
        mandate = state.buyer_mandate
        evaluation = await self._run(agent, BuyerQuoteEvaluation, state.buyer_id, {
            "demo_only": state.demo,
            "task": "Recommend accept, counter or escalate for this indicative quote.",
            "quote": {"text": quote_message.get("text"), **quote},
            "buyer_mandate": {"price_ceiling": mandate.price_ceiling, "currency": mandate.currency,
                              "rounds_left": max(mandate.max_rounds - state.rounds, 0)},
            "context_pack": pack,
        })
        refs, dropped = filter_refs(evaluation.refs, set(collect_refs(pack)) | recorder.seen_refs)
        decision = decide_on_quote(evaluation=evaluation, quote=quote, mandate=mandate,
                                   rounds_used=state.rounds, cited_refs=refs)
        base_payload = {
            "model_recommendation": evaluation.recommendation,
            "overridden_by_mandate": decision.overridden,
            "reasons": decision.reasons,
            "mandate_source": mandate.source,
            "demo": state.demo,
            "dropped_refs": dropped,
            "binding": False,
        }
        if decision.action == "accept":
            text = evaluation.message
            payload = {**base_payload, "unit_price": quote["unit_price"], "currency": quote["currency"],
                       "quantity": quote["quantity"], "total_price": quote["total_price"],
                       "quote_message_id": quote_message.get("id")}
        elif decision.action == "counter":
            counter = decision.counter_unit_price
            text = evaluation.message if not decision.overridden else (
                f"Counter-offer: {counter:g} {quote['currency']} per {quote.get('quantity_unit') or 'unit'}, "
                "within the buyer mandate. Indicative and non-binding."
            )
            payload = {**base_payload, "counter_unit_price": counter, "currency": quote["currency"],
                       "quantity": quote["quantity"], "round": state.rounds + 1}
        else:
            text = evaluation.message if not decision.overridden else (
                "Escalated to a human: " + "; ".join(decision.reasons)
            )
            payload = base_payload
        message = await self._append(state, "buyer", state.buyer_id, decision.action, text, payload,
                                     refs, model=self.model)
        await self._log_tools(state, message, [prefetch], recorder)

        if decision.action == "accept":
            await self._close_accepted(state, accepted_by="buyer")
        elif decision.action == "counter":
            state.rounds += 1
            state.last_buyer_message = message
        else:
            state.status = "escalated"
        return decision.action

    async def _close_accepted(
        self, state: DealState, *, accepted_by: str, terms: dict[str, Any] | None = None
    ) -> None:
        """Closes with the accepted terms: the accept payload when it states a price, else the last quote."""
        quote = (state.last_quote or {}).get("payload") or {}
        accepted = self._quote_from_payload({"payload": terms or {}})
        if accepted is not None:
            quote = {**quote, **accepted}
        # deal_outcomes keys (final_price, unit_price, currency, quantity, unit) record the agreed
        # indicative price now; delivery and quality are reported later via /outcome.
        outcome = {
            key: value
            for key, value in {
                "final_price": quote.get("total_price"),
                "unit_price": quote.get("unit_price"),
                "currency": quote.get("currency"),
                "quantity": quote.get("quantity"),
                "unit": quote.get("quantity_unit"),
                "accepted_by": accepted_by,
                "rounds": state.rounds,
                "indicative": True,
                "demo": state.demo,
            }.items()
            if value is not None
        }
        await self.client.close_conversation(state.conversation_id, outcome)
        state.status = "closed"
        state.steps.append({"step": "closed", "outcome": outcome})

    # ----------------------------------------------------------------- helpers

    def _tools(self, state: DealState, role: str, recorder: ToolCallRecorder) -> list[Any]:
        viewer = state.supplier_id if role == "supplier" else state.buyer_id
        return build_knowledge_tools(
            KnowledgeToolContext(
                client=self.client,
                embedder=self.embedder,
                role=role,
                viewer_id=viewer,
                conversation_id=state.conversation_id,
                recorder=recorder,
            )
        )

    async def _run(self, agent: Agent, output_model: type[OutputModel], user_id: str,
                   payload: dict[str, Any]) -> OutputModel:
        runner = self._run_agent or run_structured_agent
        result = await runner(agent, output_model, user_id=user_id, payload=payload)
        if not isinstance(result, output_model):
            try:
                result = output_model.model_validate(result)
            except ValidationError as error:
                raise AgentRuntimeError("ADK agent returned invalid structured output") from error
        return result

    async def _embed(self, text: str | None) -> list[float] | None:
        return await self.embedder.embed(text) if self.embedder is not None and text else None

    async def _append(self, state: DealState, sender_role: str, sender_id: str, message_type: str,
                      text: str | None, payload: dict[str, Any], refs: list[str],
                      *, model: str | None = None) -> dict[str, Any]:
        payload = {key: value for key, value in payload.items() if value not in (None, [], {})}
        message = await self.client.append_message(state.conversation_id, {
            "sender_role": sender_role,
            "sender_id": sender_id,
            "type": message_type,
            "text": text,
            "payload": payload,
            "refs": refs,
            "model": model,
            "agent_version": AGENT_VERSION if model else None,
        })
        state.steps.append({"step": message_type, "sender_role": sender_role,
                            "message_id": message.get("id"), "seq": message.get("seq")})
        return message

    @staticmethod
    def _prefetch_record(role: str, purpose: str, pack: dict[str, Any], started: float,
                         **subject: Any) -> dict[str, Any]:
        return {
            "tool": "agent_context",
            "args": {"role": role, "purpose": purpose, "prefetch": True,
                     **{key: value for key, value in subject.items() if value}},
            "result_refs": collect_refs(pack),
            "latency_ms": int((time.perf_counter() - started) * 1000),
        }

    async def _log_tools(self, state: DealState, message: dict[str, Any],
                         prefetches: list[dict[str, Any]], recorder: ToolCallRecorder | None) -> None:
        calls = list(prefetches)
        for call in recorder.drain() if recorder else []:
            args = dict(call.args)
            if call.error:
                args["error"] = call.error
            calls.append({"tool": call.tool, "args": args, "result_refs": call.result_refs,
                          "latency_ms": call.latency_ms})
        for call in calls:
            try:
                await self.client.log_tool_call(conversation_id=state.conversation_id,
                                                message_id=message.get("id"), **call)
            except KnowledgeError as error:  # logging must not break a conversation
                logger.warning("Could not log tool call %s: %s", call["tool"], error)

    async def _resolve_mandate(self, requested: Mandate | None, company_ids: list[str]) -> Mandate:
        if requested is not None:
            return requested.model_copy(update={"source": requested.source or "caller-supplied mandate"})
        row = await self.client.active_mandate([company_id for company_id in company_ids if company_id])
        if row:
            fields = ("max_autonomous_value_eur", "min_supplier_score", "allowed_kyc_status", "allowed_cpv_prefixes")
            return Mandate(source="signed agent mandate", **{k: row[k] for k in fields if row.get(k) is not None})
        return Mandate(source=DEFAULT_MANDATE_SOURCE)

    def _require_opt_in(self, request: CreateConversationRequest, tender: dict[str, Any]) -> None:
        if self.profiles is None:
            raise ConversationPolicyError("Profiles are not loaded; only demo conversations can run")
        supplier = self.profiles.get_supplier(request.supplier_id)
        settings = supplier.agent_settings if supplier else None
        cpv_codes = set(tender.get("cpv_codes") or [])
        if settings is None or not settings.enabled or not cpv_codes.intersection(settings.opted_in_categories):
            raise ConversationPolicyError("Supplier agent is not opted in for this demand category")
        buyer = self.profiles.get_buyer(request.buyer_id)
        policy = buyer.agent_policy if buyer else None
        if policy is None or policy.status != "active" or "request_quote" not in policy.permitted_actions:
            raise ConversationPolicyError("Buyer has no active, buyer-approved agent policy for quote requests")

    async def _load_state(self, conversation_id: str) -> DealState:
        view = await self.client.get_conversation(conversation_id)
        if view is None:
            raise ConversationNotFoundError(f"Conversation {conversation_id} was not found")
        conversation = view.get("conversation") or {}
        if conversation.get("closed_at"):
            raise ConversationPolicyError("Conversation is closed")
        messages = view.get("messages") or []
        quotes = [m for m in messages if m.get("type") == "quote"]
        offers = [m for m in messages if m.get("type") in {"quote", "counter"}]
        buyer_turns = [m for m in messages if m.get("sender_role") == "buyer" and m.get("type") in {"rfq", "counter"}]
        return DealState(
            conversation_id=conversation_id,
            publication_number=conversation.get("publication_number") or "",
            buyer_id=conversation.get("buyer_id") or "",
            supplier_id=conversation.get("supplier_id") or "",
            buyer_mandate=self._stored_mandate(conversation.get("buyer_mandate")),
            supplier_mandate=self._stored_mandate(conversation.get("supplier_mandate")),
            demo=bool(conversation.get("demo", True)),
            rounds=sum(1 for m in messages if m.get("type") == "counter"),
            last_quote=quotes[-1] if quotes else None,
            last_buyer_message=buyer_turns[-1] if buyer_turns else None,
            pending_counter=offers[-1] if offers and offers[-1].get("type") == "counter" else None,
            status=conversation.get("status") or "open",
        )

    @staticmethod
    def _stored_mandate(value: Any) -> Mandate:
        if isinstance(value, dict):
            try:
                return Mandate.model_validate(value)
            except ValidationError:
                logger.warning("Stored mandate is invalid; using the default platform policy")
        return Mandate(source=DEFAULT_MANDATE_SOURCE)

    @staticmethod
    def _quote_from_payload(message: dict[str, Any]) -> dict[str, Any] | None:
        payload = message.get("payload") or {}
        unit_price, quantity = _number(payload.get("unit_price")), _number(payload.get("quantity"))
        currency = str(payload.get("currency") or "").upper()
        if unit_price is None or unit_price <= 0 or quantity is None or quantity <= 0 \
                or not re.fullmatch(r"[A-Z]{3}", currency):
            return None
        return {"unit_price": unit_price, "quantity": quantity, "currency": currency,
                "total_price": round(unit_price * quantity, 2)}

    @staticmethod
    def _counter_terms(counter: dict[str, Any], last_quote: dict[str, Any] | None) -> dict[str, Any]:
        """Terms of a buyer counter: its unit price with the quantity and currency of the quote."""
        counter_payload = counter.get("payload") or {}
        quote_payload = (last_quote or {}).get("payload") or {}
        return {
            "unit_price": counter_payload.get("counter_unit_price"),
            "currency": counter_payload.get("currency") or quote_payload.get("currency"),
            "quantity": counter_payload.get("quantity") or quote_payload.get("quantity"),
        }

    def _agent_failure_guard(self, state: DealState) -> "_AgentFailureGuard":
        return _AgentFailureGuard(self, state)

    async def _result(self, state: DealState) -> dict[str, Any]:
        view = await self.client.get_conversation(state.conversation_id) or {}
        conversation = view.get("conversation") or {}
        return {
            "conversation_id": state.conversation_id,
            "status": conversation.get("status") or state.status,
            "demo": state.demo,
            "steps": state.steps,
            "conversation": view,
        }


class _AgentFailureGuard:
    """Records an agent failure as a system escalation, then raises ``ConversationAgentError``."""

    def __init__(self, orchestrator: ConversationOrchestrator, state: DealState) -> None:
        self.orchestrator = orchestrator
        self.state = state

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: Any, error: BaseException | None, traceback: Any) -> bool:
        if not isinstance(error, AgentRuntimeError):
            return False
        detail = f"{error}: {error.provider_detail}" if error.provider_detail else str(error)
        try:
            await self.orchestrator._append(
                self.state, "system", POLICY_SENDER_ID, "escalate",
                "Escalated: an agent turn failed.", {"reasons": [str(error)]}, [],
            )
            self.state.status = "escalated"
        except KnowledgeError as append_error:
            logger.warning("Could not record the agent failure: %s", append_error)
        raise ConversationAgentError(detail, self.state.conversation_id) from error
=======
        client: KnowledgeClient,
        profiles: dict[str, dict[str, Any]],
        model: str,
        runner: Runner = run_structured_agent,
        max_agent_messages: int = 10,
        review_gate: bool = False,
        clarify_first: bool = False,
    ) -> None:
        self.client = client
        self.profiles = profiles
        self.model = model
        self.runner = runner
        self.max_agent_messages = max_agent_messages
        # True: pause after the supplier's opening message until the buyer reviewed the checks.
        self.review_gate = review_gate
        # True: the buyer agent's first turn asks one question about the deal check before it may accept.
        self.clarify_first = clarify_first
        self.tasks: set[asyncio.Task] = set()
        self.running: set[str] = set()

    def spawn(self, coroutine: Awaitable[Any]) -> None:
        """Run in the background; keep a reference so the task is not garbage-collected."""
        task = asyncio.ensure_future(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # ------------------------------------------------------------------ mandates

    def mandates(self, tender: dict[str, Any], supplier: dict[str, Any], context: dict[str, Any]):
        dd = initial_due_diligence(supplier)
        flags = (supplier.get("capabilities") or {}).get("risk_flags") or []
        reasons = []
        if dd.decision == "BLOCKED":
            reasons.append("due diligence blocked")
        if dd.score < 6:
            reasons.append(f"due diligence {dd.score}/10")
        if flags:
            reasons.append(f"{len(flags)} risk flag(s) on the supplier")
        deal_check = tender.get("deal_check") or {}
        if deal_check.get("decision") in ("ESCALATE", "NO-GO"):
            reasons.append(f"deal check {deal_check['decision']} ({len(deal_check.get('blocking') or [])} blocking item(s))")
        anchor = next(iter(context.get("price_anchors") or []), None)
        listed = None if anchor else price_list_entry(supplier["id"], tender)
        if listed:
            anchor = {"unit_price": listed["unit_price"], "currency": listed["currency"]}
        budget = _number(tender.get("budget"))
        rules = {
            "budget": budget,
            "currency": tender.get("currency") or "EUR",
            "quantity": _number(tender.get("quantity")),
            "max_counters": MAX_COUNTERS,
            "needs_person": "; ".join(reasons),
            "min_unit_price": round(anchor["unit_price"] * PRICE_FLOOR, 4) if anchor else None,
            "price_currency": anchor["currency"] if anchor else None,
            "review_gate": self.review_gate,
        }
        buyer_mandate = {
            "budget": f"{tender.get('currency', 'EUR')} {budget:,.0f} total" if budget else "not set",
            "max_counter_offers": MAX_COUNTERS,
            "min_supplier_evidence": "document_verified (proven on TED)",
            "person_must_approve": rules["needs_person"] or "deals outside this mandate",
        }
        supplier_mandate = {
            "may_quote": "products in your catalogue only",
            "price_floor": (f"{anchor['currency']} {rules['min_unit_price']} per unit (awarded TED price -3%)"
                            if anchor and not listed else "no awarded price known: quote only with a stated basis"),
            "person_must_approve": "orders above EUR 1,000,000 or items you have not proven",
        }
        if listed:
            supplier_mandate["price_list"] = (
                f"{listed['item']}: {listed['currency']} {listed['unit_price']} per {listed['unit']}, "
                f"lead time {listed['lead_time_days']} days ({listed['basis']}). You may quote this price without a person; "
                f"floor {listed['currency']} {rules['min_unit_price']} per {listed['unit']}")
            supplier_mandate["price_floor"] = f"{listed['currency']} {rules['min_unit_price']} per {listed['unit']} (price list -3%)"
        return buyer_mandate, supplier_mandate, rules

    # ------------------------------------------------------------------ entry points

    async def handle_request(self, request: dict[str, Any], supplier_id: str) -> dict[str, Any] | None:
        buyer = self.profiles.get(request.get("buyer_id", ""))
        return await self.handle_tender(tender_from_request(request, buyer), supplier_id, request_id=request.get("id"))

    async def handle_tender(self, tender: dict[str, Any], supplier_id: str, request_id: str | None = None) -> dict[str, Any] | None:
        """Assess one tender for one supplier; start the conversation if it can supply."""
        supplier = self.profiles.get(supplier_id)
        if supplier is None or not tender.get("buyer_id"):
            return None
        context = await self.client.context("supplier", "assess_tender", {"tender": tender, "supplier_id": supplier_id},
                                            viewer_id=supplier_id)
        buyer_mandate, supplier_mandate, rules = self.mandates(tender, supplier, context)
        agent = build_company_agent(supplier, "supplier", "assess", supplier_mandate, self.model)
        base = {"request_id": request_id or tender["ref"], "tender_ref": tender["ref"], "title": tender["title"],
                "buyer_id": tender["buyer_id"], "supplier_id": supplier_id, "model": self.model}
        try:
            assessment = await self.runner(agent, SupplierAssessment, user_id=supplier_id,
                                           payload={"request": tender_facts(tender, "supplier"), "context": context})
        except AgentRuntimeError as error:
            return await self.client.record_assessment({**base, "error": str(error)})
        record = await self.client.record_assessment({**base, **assessment.model_dump()})
        if not assessment.can_supply:
            return record

        conversation = await self.client.create_conversation({
            "publication_number": tender["ref"],
            "title": tender["title"],
            "request": tender,
            "buyer_id": tender["buyer_id"],
            "supplier_id": supplier_id,
            "family_id": (context.get("facts", {}).get("tender") or {}).get("family"),
            "buyer_mandate": buyer_mandate,
            "supplier_mandate": supplier_mandate,
            "rules": rules,
            "assessment_id": record["id"],
        })
        await self.client.append_message(conversation["id"], {
            "sender_role": "system", "sender_id": "platform", "type": "check",
            "text": (f"{supplier['name']} agent assessed the request: can supply ({assessment.confidence} confidence). "
                     f"{assessment.reasoning}"),
            "payload": {"assessment": assessment.model_dump()},
            "refs": assessment.refs,
        })
        await self.run_conversation(conversation["id"], "supplier")
        return record

    async def resume(self, conversation_id: str) -> dict[str, Any]:
        """The buyer reviewed due diligence and the deal check: its agent may now negotiate."""
        conversation = await self.client.get_conversation(conversation_id)
        if conversation is None:
            raise KeyError(conversation_id)
        if conversation["status"] == "awaiting_review":
            await self.client.update_conversation(conversation_id, {"status": "open", "buyer_reviewed": True})
            await self._system(conversation_id, "check", "The buyer reviewed due diligence and the D1–D14 deal check; its agent negotiates now.")
            self.spawn(self.run_conversation(conversation_id, "buyer"))
        return await self.client.get_conversation(conversation_id)

    async def approve(self, conversation_id: str, decision: str, approver: str) -> dict[str, Any]:
        """A person on the buyer's side approves or rejects the deal the agents agreed (the only human step)."""
        conversation = await self.client.get_conversation(conversation_id)
        if conversation is None:
            raise KeyError(conversation_id)
        if conversation["status"] != "accepted":
            raise ValueError("only a deal the agents agreed can be approved")
        if decision not in ("approved", "rejected"):
            raise ValueError("decision must be approved or rejected")
        await self.client.update_conversation(conversation_id, {"approval": {"decision": decision, "approver": approver, "at": now()}})
        await self._system(conversation_id, "check", f"Deal {decision} by {approver} (buyer).")
        return await self.client.get_conversation(conversation_id)

    async def human_message(self, conversation_id: str, sender_role: str, sender_id: str, text: str) -> dict[str, Any]:
        conversation = await self.client.get_conversation(conversation_id)
        if conversation is None:
            raise KeyError(conversation_id)
        message = await self.client.append_message(conversation_id, {
            "sender_role": sender_role, "sender_id": sender_id,
            "type": "question" if sender_role == "buyer" else "answer",
            "text": text, "payload": {"human": True},
        })
        if conversation["status"] not in TERMINAL:
            if conversation["status"] in ("escalated", "awaiting_review"):
                # A person writing counts as the buyer's review.
                await self.client.update_conversation(conversation_id, {"status": "open", "buyer_reviewed": True})
            self.spawn(self.run_conversation(conversation_id, "supplier" if sender_role == "buyer" else "buyer"))
        return message

    # ------------------------------------------------------------------ turn loop

    async def run_conversation(self, conversation_id: str, next_role: str) -> None:
        if conversation_id in self.running:
            return
        self.running.add(conversation_id)
        try:
            while True:
                conversation = await self.client.get_conversation(conversation_id)
                if conversation is None or conversation["status"] in TERMINAL | {"escalated", "awaiting_review"}:
                    return
                if (next_role == "buyer" and conversation.get("rules", {}).get("review_gate")
                        and not conversation.get("buyer_reviewed")):
                    # Pause after the supplier's opening message until the buyer has reviewed the checks.
                    await self.client.update_conversation(conversation_id, {"status": "awaiting_review"})
                    return
                agent_messages = [m for m in conversation["messages"]
                                  if m["sender_role"] in ("buyer", "supplier") and not m["payload"].get("human")]
                if len(agent_messages) >= self.max_agent_messages:
                    await self._system(conversation_id, "escalate", "Turn limit reached; a person from each side takes over.")
                    await self.client.update_conversation(conversation_id, {"status": "escalated", "typing": None})
                    return
                if next_role == "buyer" and not any(m["sender_role"] == "buyer" for m in conversation["messages"]):
                    # Before its first reply the buyer agent runs due diligence on this supplier and this deal.
                    await self._buyer_checks(conversation)
                    conversation = await self.client.get_conversation(conversation_id)
                await self.client.update_conversation(conversation_id, {"typing": next_role})
                turn = await self._turn(conversation, next_role)
                if await self._apply(conversation, next_role, turn):
                    return
                next_role = "buyer" if next_role == "supplier" else "supplier"
        except AgentRuntimeError as error:
            await self._system(conversation_id, "escalate", f"The agent could not answer ({error}). A person takes over.")
            await self.client.update_conversation(conversation_id, {"status": "escalated"})
        finally:
            self.running.discard(conversation_id)
            await self.client.update_conversation(conversation_id, {"typing": None})

    async def _turn(self, conversation: dict[str, Any], role: str) -> AgentTurn:
        profile = self.profiles[conversation[f"{role}_id"]]
        context = await self.client.context(
            role, "assess_tender" if role == "supplier" else "evaluate_quote",
            {"tender": conversation["request"], "supplier_id": conversation["supplier_id"]}, viewer_id=profile["id"])
        history = [{
            "from": f"{m['sender_role']} (person)" if m["payload"].get("human") else m["sender_role"],
            "type": m["type"], "text": m["text"],
            "terms": {k: v for k, v in m["payload"].items() if k in ("unit_price", "currency", "quantity", "lead_time_days")},
        } for m in conversation["messages"][-12:]]
        agent = build_company_agent(profile, role, "turn", conversation[f"{role}_mandate"], self.model)
        payload = {
            "you_are": f"the {role} agent of {profile['name']}",
            "request": tender_facts(conversation["request"], role),
            "conversation": history,
            "context": context,
        }
        if (role == "buyer" and (conversation.get("buyer_reviewed") or self.clarify_first)
                and not any(m["sender_role"] == "buyer" for m in conversation["messages"])):
            # First buyer turn: clarify the deal check before any decision.
            payload["instruction"] = (
                "Before deciding, check this supplier's due diligence and the D1-D14 deal check "
                "(request.deal_check). Do not accept yet. Ask the supplier one short question about the most important "
                "open condition in the deal check (e.g. transfer licence, end-user certificate, delivery schedule, "
                "quality certificate), and confirm what you need before you can accept.")
        if role == "buyer":
            # The buyer talks to several suppliers about one request; it sees their latest terms.
            payload["other_offers"] = [
                {"supplier": self.profiles[c["supplier_id"]]["name"], "status": c["status"], **self._latest_quote(c)}
                for c in await self._siblings(conversation)
            ]
        return await self.runner(agent, AgentTurn, user_id=f"{conversation['id']}:{role}", payload=payload)

    async def _siblings(self, conversation: dict[str, Any]) -> list[dict[str, Any]]:
        """Other conversations of the same buyer about the same request."""
        return [c for c in await self.client.list_conversations(conversation["buyer_id"])
                if c["publication_number"] == conversation["publication_number"] and c["id"] != conversation["id"]]

    @staticmethod
    def _latest_quote(conversation: dict[str, Any]) -> dict[str, Any]:
        return next((
            {k: v for k, v in m["payload"].items() if k in ("unit_price", "currency", "quantity", "lead_time_days")}
            for m in reversed(conversation["messages"])
            if m["sender_role"] == "supplier" and m["payload"].get("unit_price")), {})

    async def _apply(self, conversation: dict[str, Any], role: str, turn: AgentTurn) -> bool:
        """Enforce the mandate, persist the turn and update the status. True when the conversation ends."""
        rules = conversation["rules"]
        allowed = SUPPLIER_TURN_TYPES if role == "supplier" else BUYER_TURN_TYPES
        kind = turn.type if turn.type in allowed else ("answer" if role == "supplier" else "question")
        terms = {k: v for k, v in {"unit_price": turn.unit_price, "currency": turn.currency,
                                   "quantity": turn.quantity, "lead_time_days": turn.lead_time_days}.items() if v}
        notes = []
        quote = next((m["payload"] for m in reversed(conversation["messages"])
                      if m["sender_role"] == "supplier" and m["type"] in ("quote", "counter") and m["payload"].get("unit_price")), None)

        if role == "buyer":
            counters = sum(1 for m in conversation["messages"] if m["sender_role"] == "buyer" and m["type"] == "counter")
            if kind == "counter" and counters >= rules["max_counters"]:
                notes.append(f"Counter-offer limit ({rules['max_counters']}) reached.")
                kind = "escalate"
            awarded = [c for c in await self._siblings(conversation) if c["status"] == "accepted"] if kind == "accept" else []
            if awarded:
                notes.append(f"{self.profiles[awarded[0]['supplier_id']]['name']} already has the agreed deal for this request; "
                             "a person decides on a second award.")
                kind = "escalate"
            if kind == "accept":
                if not quote:
                    notes.append("A deal needs a quote first, so the buyer agent asked for one.")
                    kind = "rfq"
                else:
                    quantity = _number(quote.get("quantity")) or rules.get("quantity")
                    total = quote["unit_price"] * quantity if quantity else None
                    currency = quote.get("currency") or rules["currency"]
                    if rules.get("budget") and total and currency == rules["currency"] and total > rules["budget"]:
                        notes.append(f"Total {currency} {total:,.0f} is above the budget of {rules['currency']} {rules['budget']:,.0f}.")
                        kind = "escalate"
                    elif rules.get("needs_person"):
                        notes.append(f"The buyer's mandate needs a person to approve: {rules['needs_person']}.")
                        kind = "escalate"
                    else:
                        terms = {k: v for k, v in quote.items() if k != "overridden_from"}
                        if total:
                            terms["total"] = round(total, 2)
        if role == "supplier" and kind in ("quote", "counter") and terms.get("unit_price") and rules.get("min_unit_price"):
            if (terms.get("currency") or rules["price_currency"]) == rules["price_currency"] and terms["unit_price"] < rules["min_unit_price"]:
                notes.append(f"Quote is below the supplier's price floor ({rules['price_currency']} {rules['min_unit_price']}).")
                kind = "escalate"

        payload = {**terms, **({"overridden_from": turn.type} if kind != turn.type else {})}
        await self.client.append_message(conversation["id"], {
            "sender_role": role, "sender_id": conversation[f"{role}_id"], "type": kind, "text": turn.text,
            "payload": payload, "refs": turn.refs, "model": self.model, "agent_version": AGENT_VERSION,
        })
        if notes:
            await self._system(conversation["id"], "check", " ".join(notes))
        if kind == "accept":
            await self.client.close_conversation(conversation["id"], "accepted", {**terms, "accepted_by": role})
        elif kind == "reject":
            await self.client.close_conversation(conversation["id"], "rejected", {"rejected_by": role})
        elif kind == "escalate":
            await self.client.update_conversation(conversation["id"], {"status": "escalated"})
        elif kind in ("quote", "counter") and role == "supplier":
            await self.client.update_conversation(conversation["id"], {"status": "quoted"})
        return kind in ENDING_TYPES

    async def _buyer_checks(self, conversation: dict[str, Any]) -> None:
        """Buyer agent's due diligence before it answers: company score + KYC, and the D1-D14 deal check."""
        if any((m.get("payload") or {}).get("buyer_checks") for m in conversation["messages"]):
            return
        supplier = self.profiles[conversation["supplier_id"]]
        dd = initial_due_diligence(supplier)
        deal = conversation["request"].get("deal_check") or {}
        checks = deal.get("checks") or []
        summary = {
            "due_diligence": {
                "score": dd.score, "verdict": dd.verdict, "decision": dd.decision, "confidence": dd.confidence,
                "kyc_status": dd.kyc_status, "red_flags": dd.red_flags[:3],
                "gates": [{"title": g.title, "status": g.status} for g in dd.gates],
            },
            "deal_check": {
                "decision": deal.get("decision"),
                "counts": {s: sum(1 for c in checks if c.get("status") == s) for s in ("pass", "condition", "escalate", "fail", "info")},
                "blocking": (deal.get("blocking") or [])[:3],
                "conditions": len(deal.get("conditions") or []),
            },
        }
        passed = sum(1 for g in dd.gates if g.status == "pass")
        text = (f"{self.profiles[conversation['buyer_id']]['name']} agent ran due diligence before replying: "
                f"{supplier['name']} scores {dd.score}/10 ({dd.verdict}, {dd.decision.lower().replace('_', ' ')}), "
                f"{passed}/{len(dd.gates)} hard gates passed, KYC {dd.kyc_status}. "
                f"Deal check D1–D14: {deal.get('decision') or 'not available'}"
                + (f", {len(deal.get('conditions') or [])} contract condition(s)." if deal else "."))
        await self.client.append_message(conversation["id"], {
            "sender_role": "system", "sender_id": conversation["buyer_id"], "type": "check", "text": text,
            "payload": {"buyer_checks": summary},
        })

    async def _system(self, conversation_id: str, kind: str, text: str) -> None:
        await self.client.append_message(conversation_id, {
            "sender_role": "system", "sender_id": "platform", "type": kind, "text": text})
>>>>>>> 948dddf (s)
