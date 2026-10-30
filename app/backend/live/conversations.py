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
from app.backend.live.client import KnowledgeClient
from app.backend.live.context import tender_facts
from app.backend.live.simulated import now

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
