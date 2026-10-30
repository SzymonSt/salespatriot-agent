"""In-memory stand-ins for the knowledge RPCs (contract §3) used by the WS5 tests."""

import copy
import uuid
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from app.backend.knowledge.client import KnowledgeRejectedError

PUB = "630124-2026"
SUPPLIER = "supplier-cze-65414128"
BUYER = "buyer:Generálne riaditeľstvo Zboru väzenskej a justičnej stráže"
TENDER_REF = f"ted:{PUB}"
CONTRACT_REF = f"contract:{SUPPLIER}/123456-2025/LOT-1"
AWARD_REF = "award:720852-2026/cenzin sp. z o.o."


def tender_facts(**overrides: Any) -> dict[str, Any]:
    return {
        "ref": TENDER_REF,
        "title": "Ammunition 9x19",
        "buyer": "Generálne riaditeľstvo Zboru väzenskej a justičnej stráže",
        "country": "SVK",
        "families": [{"family_id": "ammunition.small_calibre", "calibre": ["9x19"],
                      "method": "rules", "confidence": 0.8}],
        "estimated_value": 100_000,
        "currency": "EUR",
        **overrides,
    }


def default_pack(purpose: str, subject: dict[str, Any]) -> dict[str, Any]:
    pack: dict[str, Any] = {
        "purpose": purpose,
        "subject": subject,
        "facts": {"tender": tender_facts()},
        "candidates": [],
        "evidence": [{"ref": CONTRACT_REF, "kind": "contract", "title": "9x19 lot", "snippet": "…",
                      "score": 0.8, "layer": "ted_live"}],
        "price_anchors": [{"ref": CONTRACT_REF, "family_id": "ammunition.small_calibre", "calibre": "9x19",
                           "value": 60_000, "currency": "EUR", "quantity": "50000 pcs",
                           "basis": "awarded_lot_value", "layer": "ted_live"}],
        "regulatory": {"eu_ml": "ML3", "verified": False},
        "memory": [],
        "warnings": [],
        "refs": [TENDER_REF, CONTRACT_REF],
        "versions": {"context": "v1"},
    }
    if purpose == "find_suppliers":
        pack["candidates"] = [{"supplier_id": SUPPLIER, "ref": f"supplier:{SUPPLIER}", "proven_lots": 3}]
        pack["evidence"].append({"ref": AWARD_REF, "kind": "award", "title": "award", "snippet": "…",
                                 "score": 0.5, "layer": "ted_live"})
        pack["refs"].extend([f"supplier:{SUPPLIER}", AWARD_REF])
    return pack


def default_facts(**overview: Any) -> dict[str, Any]:
    return {
        "overview": {"supplier_id": SUPPLIER, "name": "C.I.D Praha s.r.o.", "total_score": 8,
                     "kyc_status": "limited", **overview},
        "tender": {"publication_number": PUB, "buyer_name": "Generálne riaditeľstvo Zboru väzenskej a justičnej stráže",
                   "buyer_country": "SVK", "cpv_codes": ["35321100"], "estimated_value": 100_000,
                   "estimated_currency": "EUR"},
        "deal_check": None,
    }


DEAL_KEYS = ("delivered_on_time", "quality", "final_price", "currency", "unit_price", "quantity", "unit",
             "notes", "reported_by")


class FakeKnowledgeClient:
    """Mirrors the live WS4 RPCs (agent_message_append, agent_conversation_close, memory_feedback)
    and the agent_conversations insert trigger, behind the KnowledgeClient interface."""

    def __init__(
        self,
        *,
        facts: dict[str, Any] | None = None,
        packs: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] | None = None,
        mandates: dict[str, dict[str, Any]] | None = None,
        live_tender: dict[str, Any] | None = None,
    ) -> None:
        self.facts = facts if facts is not None else default_facts()
        self.packs = packs or {}
        self.mandates = mandates or {}
        self.live_tender = live_tender
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.conversations: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.tool_calls: list[dict[str, Any]] = []
        self.outcomes: dict[str, dict[str, Any]] = {}
        self.memories: dict[str, dict[str, Any]] = {}
        self.hits: list[dict[str, Any]] = [{"ref": CONTRACT_REF, "kind": "contract", "title": "t",
                                            "snippet": "s", "facts": {}, "score": 0.03}]

    async def context(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("context", kwargs))
        builder = self.packs.get(kwargs["purpose"])
        subject = kwargs["subject"]
        return copy.deepcopy(builder(subject) if builder else default_pack(kwargs["purpose"], subject))

    async def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("search", kwargs))
        return copy.deepcopy(self.hits)

    async def recall(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append(("recall", kwargs))
        return [{"ref": "memory:11111111-1111-1111-1111-111111111111", "kind": "lesson", "text": "t"}]

    async def remember(self, entry: dict[str, Any]) -> str:
        self.calls.append(("remember", {"entry": entry}))
        memory_id = str(uuid.uuid4())
        self.memories[memory_id] = {**entry, "helpful": 0, "unhelpful": 0}
        return memory_id

    async def memory_feedback(self, memory_id: str, helpful: bool) -> dict[str, Any] | None:
        memory = self.memories.get(memory_id)
        if memory is None:
            return None
        memory["helpful" if helpful else "unhelpful"] += 1
        return {"ref": f"memory:{memory_id}", "helpful": memory["helpful"], "unhelpful": memory["unhelpful"]}

    async def family_schema(self, family_id: str) -> dict[str, Any] | None:
        self.calls.append(("family_schema", {"family_id": family_id}))
        return {"title": family_id, "required": ["calibre"]} if family_id.startswith("ammunition") else None

    async def regulatory_profile(self, family_id: str, calibre: str | None) -> dict[str, Any] | None:
        self.calls.append(("regulatory_profile", {"family_id": family_id, "calibre": calibre}))
        return {"eu_ml": [{"code": "ML3.a"}]}

    async def create_conversation(self, **kwargs: Any) -> dict[str, Any]:
        conversation_id = str(uuid.uuid4())
        if kwargs.get("family_id") is None:  # agent_conversations_fill trigger
            kwargs.update(family_id="ammunition.small_calibre", calibre="9x19", country="SVK")
        self.conversations[conversation_id] = {"id": conversation_id, "status": "open", "closed_at": None,
                                               "outcome": None, **kwargs}
        self.messages[conversation_id] = []
        return copy.deepcopy(self.conversations[conversation_id])

    async def append_message(self, conversation_id: str, message: dict[str, Any]) -> dict[str, Any]:
        conversation = self.conversations[conversation_id]
        previous = self.messages[conversation_id]
        kind, role, status = message["type"], message["sender_role"], conversation["status"]
        offers = [m["sender_role"] for m in previous if m["type"] in {"quote", "counter"}]

        def illegal(reason: str) -> KnowledgeRejectedError:
            return KnowledgeRejectedError(f"agent_message_append was rejected: illegal transition: {reason}")

        if status == "closed":
            raise KnowledgeRejectedError(f"agent_message_append was rejected: conversation {conversation_id} is closed")
        if kind == "rfq":
            if status != "open" or role == "supplier":
                raise illegal(f"rfq while {status}")
        elif kind in {"quote", "counter"}:
            if not any(m["type"] == "rfq" for m in previous) or status not in {"open", "quoted", "escalated"}:
                raise illegal(f"{kind} before an rfq or while {status}")
            if kind == "quote" and role == "buyer":
                raise illegal("a quote comes from the supplier")
            status = "quoted"
        elif kind == "accept":
            if not offers or status not in {"quoted", "escalated"} or (role != "system" and role == offers[-1]):
                raise illegal(f"accept while {status}")
            status = "accepted"
        elif kind == "reject":
            if status not in {"open", "quoted", "escalated"}:
                raise illegal(f"reject while {status}")
            status = "rejected"
        elif kind == "escalate":
            if status not in {"open", "quoted", "accepted", "escalated"}:
                raise illegal(f"escalate while {status}")
            status = "escalated"
        result = {"id": str(uuid.uuid4()), "conversation_id": conversation_id, "seq": len(previous) + 1,
                  "previous_status": conversation["status"], "status": status}
        conversation["status"] = status
        self.messages[conversation_id].append({**copy.deepcopy(message), **result})
        return {"conversation_id": conversation_id, **copy.deepcopy(message), **result}

    async def close_conversation(self, conversation_id: str, outcome: dict[str, Any]) -> dict[str, Any]:
        conversation = self.conversations[conversation_id]
        final_status = conversation["status"] if conversation["status"] != "closed" else (
            (conversation["outcome"] or {}).get("final_status", "closed"))
        if any(key in outcome for key in DEAL_KEYS):
            stored = self.outcomes.get(conversation_id, {"conversation_id": conversation_id})
            stored.update({key: outcome[key] for key in DEAL_KEYS if outcome.get(key) is not None})
            self.outcomes[conversation_id] = stored
        conversation.update(status="closed", closed_at="2026-10-30T12:00:00Z",
                            outcome={**(conversation["outcome"] or {}), **outcome, "final_status": final_status})
        return {"conversation_id": conversation_id, "status": "closed", "final_status": final_status,
                "episode_ref": f"memory:{uuid.uuid4()}"}

    async def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        if conversation_id not in self.conversations:
            return None
        return copy.deepcopy({
            "conversation": self.conversations[conversation_id],
            "messages": self.messages[conversation_id],
            "tool_calls": [c for c in self.tool_calls if c["conversation_id"] == conversation_id],
            "outcome": self.outcomes.get(conversation_id),
        })

    async def log_tool_call(self, **kwargs: Any) -> None:
        self.tool_calls.append(copy.deepcopy(kwargs))

    async def active_mandate(self, company_ids: list[str]) -> dict[str, Any] | None:
        self.calls.append(("active_mandate", {"company_ids": list(company_ids)}))
        return next((copy.deepcopy(self.mandates[i]) for i in company_ids if i in self.mandates), None)

    async def counterparty_facts(self, publication_number: str, supplier_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.facts)

    async def pick_live_tender(self, publication_number: str | None = None,
                               supplier_id: str | None = None) -> dict[str, Any] | None:
        return copy.deepcopy(self.live_tender)


class FakeEmbedder:
    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = vector if vector is not None else [1.0] + [0.0] * 767
        self.queries: list[str | None] = []

    async def embed(self, text: str | None) -> list[float] | None:
        self.queries.append(text)
        return self.vector if text else None


class ScriptedRunner:
    """Stands in for ``run_structured_agent``: returns scripted outputs per output model.

    A script item may be a model instance or an async callable ``(agent, payload) -> model``,
    which can call ``agent.tools`` like the model would.
    """

    def __init__(self, script: dict[type[BaseModel], list[Any]]) -> None:
        self.script = {model: list(items) for model, items in script.items()}
        self.calls: list[tuple[str, type[BaseModel], dict[str, Any]]] = []

    async def __call__(self, agent: Any, output_model: type[BaseModel], user_id: str,
                       payload: dict[str, Any]) -> BaseModel:
        self.calls.append((agent.name, output_model, payload))
        items = self.script.get(output_model)
        if not items:
            raise AssertionError(f"Unexpected agent turn for {output_model.__name__}")
        item = items.pop(0) if len(items) > 1 else items[0]
        if callable(item) and not isinstance(item, BaseModel):
            return await item(agent, payload)
        return item

    def models_called(self) -> list[str]:
        return [model.__name__ for _, model, _ in self.calls]
