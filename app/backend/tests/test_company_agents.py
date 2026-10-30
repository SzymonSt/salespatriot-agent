"""Orchestrator state machine with a fake model (no Gemini calls)."""

import json

import pytest

from app.backend.agents.company import AgentTurn, SupplierAssessment, build_company_agent
from app.backend.knowledge import ConversationOrchestrator, SimulatedKnowledgeClient
from app.backend.settings import REPOSITORY_ROOT

PROFILES = {
    p["id"]: p
    for name in ("suppliers.json", "buyers.json")
    for p in json.loads((REPOSITORY_ROOT / "data" / "profiles" / name).read_text(encoding="utf-8"))
}
BUYER = "buyer-deu-991-19518-88"
CLEAN_SUPPLIER = "supplier-ita-n0057381f"     # Fiocchi: no risk flags, due diligence 7.9
FLAGGED_SUPPLIER = "supplier-rou-ro-15148839"  # Transcarpat: two risk flags
REQUEST = {"id": "REQ-1", "buyer_id": BUYER, "title": "7.62x51 mm NATO ball cartridges, linked",
           "cpv_codes": ["35331500"], "quantity": 100000, "unit": "rounds", "budget": 250000, "currency": "EUR"}


def fake_runner(script):
    """Return queued outputs: assessment first, then turns in order."""
    calls = []

    async def run(agent, output_model, user_id, payload):
        calls.append((agent.name, output_model.__name__, payload))
        return script.pop(0)

    run.calls = calls
    return run


def orchestrator(tmp_path, script):
    client = SimulatedKnowledgeClient(tmp_path / "agent_db.json", PROFILES)
    return ConversationOrchestrator(client, PROFILES, "fake-model", runner=fake_runner(script)), client


def assessment(can_supply=True):
    return SupplierAssessment(can_supply=can_supply, confidence="high", matched_products=["7.62x51 NATO"],
                              reasoning="Delivered 7.62x51 to the Spanish Army.", refs=["catalogue:x/0"])


async def test_cannot_supply_records_assessment_without_conversation(tmp_path) -> None:
    orch, client = orchestrator(tmp_path, [assessment(can_supply=False)])
    record = await orch.handle_request(REQUEST, CLEAN_SUPPLIER)

    assert record["can_supply"] is False
    assert await client.list_conversations() == []


async def test_quote_within_budget_is_accepted(tmp_path) -> None:
    orch, client = orchestrator(tmp_path, [
        assessment(),
        AgentTurn(type="introduction", text="We make 7.62x51 linked."),
        AgentTurn(type="rfq", text="Please quote 100,000 rounds."),
        AgentTurn(type="quote", text="EUR 2.10 per round.", unit_price=2.10, currency="EUR", quantity=100000, lead_time_days=60),
        AgentTurn(type="accept", text="Accepted."),
    ])
    await orch.handle_request(REQUEST, CLEAN_SUPPLIER)
    [conversation] = await client.list_conversations(BUYER)

    assert conversation["status"] == "accepted"
    assert conversation["outcome"]["total"] == pytest.approx(210000)
    # check (assessment), introduction, check (buyer agent's due diligence before its first reply), rfq, quote, accept
    assert [m["type"] for m in conversation["messages"]] == ["check", "introduction", "check", "rfq", "quote", "accept"]
    assert "buyer_checks" in conversation["messages"][2]["payload"]
    assert conversation["messages"][1]["sender_role"] == "supplier"


async def test_accept_above_budget_escalates(tmp_path) -> None:
    orch, client = orchestrator(tmp_path, [
        assessment(),
        AgentTurn(type="quote", text="EUR 3.00 per round.", unit_price=3.0, currency="EUR", quantity=100000),
        AgentTurn(type="accept", text="Accepted."),
    ])
    await orch.handle_request(REQUEST, CLEAN_SUPPLIER)
    [conversation] = await client.list_conversations()

    assert conversation["status"] == "escalated"
    assert conversation["messages"][-2]["payload"]["overridden_from"] == "accept"
    assert "above the budget" in conversation["messages"][-1]["text"]


async def test_flagged_supplier_needs_a_person_to_accept(tmp_path) -> None:
    orch, client = orchestrator(tmp_path, [
        assessment(),
        AgentTurn(type="quote", text="RON 4.0 per round.", unit_price=1.0, currency="EUR", quantity=100000),
        AgentTurn(type="accept", text="Accepted."),
    ])
    await orch.handle_request(REQUEST, FLAGGED_SUPPLIER)
    [conversation] = await client.list_conversations()

    assert conversation["status"] == "escalated"
    assert "needs a person" in conversation["messages"][-1]["text"]


async def test_accept_without_quote_becomes_rfq(tmp_path) -> None:
    orch, client = orchestrator(tmp_path, [
        assessment(),
        AgentTurn(type="introduction", text="Hello."),
        AgentTurn(type="accept", text="Deal."),
        AgentTurn(type="reject", text="We cannot meet this after all."),
    ])
    await orch.handle_request(REQUEST, CLEAN_SUPPLIER)
    [conversation] = await client.list_conversations()

    assert [m["type"] for m in conversation["messages"]][:5] == ["check", "introduction", "check", "rfq", "check"]
    assert conversation["status"] == "rejected"


async def test_human_message_reopens_and_counterpart_answers(tmp_path) -> None:
    orch, client = orchestrator(tmp_path, [
        assessment(),
        AgentTurn(type="escalate", text="This needs our sales director."),
        AgentTurn(type="question", text="Can you confirm the lead time?"),
        AgentTurn(type="answer", text="60 days."),
        AgentTurn(type="reject", text="Too slow for us."),
    ])
    await orch.handle_request(REQUEST, CLEAN_SUPPLIER)
    [conversation] = await client.list_conversations()
    assert conversation["status"] == "escalated"

    await orch.human_message(conversation["id"], "supplier", CLEAN_SUPPLIER, "Approved: go ahead.")
    await orch.run_conversation(conversation["id"], "buyer")
    conversation = await client.get_conversation(conversation["id"])

    assert conversation["messages"][2]["payload"] == {"human": True}
    assert conversation["status"] == "rejected"


def test_company_profile_is_the_system_instruction() -> None:
    supplier = PROFILES[CLEAN_SUPPLIER]
    agent = build_company_agent(supplier, "supplier", "assess", {"price_floor": "EUR 1"}, "fake-model")

    assert supplier["name"] in agent.instruction
    assert f"catalogue:{CLEAN_SUPPLIER}/0" in agent.instruction
    assert "{" not in agent.instruction
    assert agent.output_schema is SupplierAssessment


async def test_deal_check_escalate_needs_a_person_to_accept(tmp_path) -> None:
    orch, client = orchestrator(tmp_path, [
        assessment(),
        AgentTurn(type="quote", text="EUR 2.10 per round.", unit_price=2.10, currency="EUR", quantity=100000),
        AgentTurn(type="accept", text="Accepted."),
    ])
    request = {**REQUEST, "deal_check": {"decision": "ESCALATE", "blocking": ["D14: above mandate"], "conditions": [], "checks": []}}
    await orch.handle_request(request, CLEAN_SUPPLIER)
    [conversation] = await client.list_conversations()

    assert conversation["status"] == "escalated"
    assert "deal check ESCALATE" in conversation["messages"][-1]["text"]


async def test_second_accept_for_the_same_request_goes_to_a_person(tmp_path) -> None:
    orch, client = orchestrator(tmp_path, [
        assessment(),
        AgentTurn(type="quote", text="EUR 2.10.", unit_price=2.10, currency="EUR", quantity=100000),
        AgentTurn(type="accept", text="Accepted."),
        assessment(),
        AgentTurn(type="quote", text="EUR 2.15.", unit_price=2.15, currency="EUR", quantity=100000),
        AgentTurn(type="accept", text="Accepted too."),
    ])
    await orch.handle_request(REQUEST, CLEAN_SUPPLIER)
    await orch.handle_request(REQUEST, "supplier-prt-500022089")
    first, second = await client.list_conversations(BUYER)

    assert first["status"] == "accepted"
    assert second["status"] == "escalated"
    assert "already has the agreed deal" in second["messages"][-1]["text"]
    buyer_payload = orch.runner.calls[-1][2]
    assert buyer_payload["other_offers"][0]["unit_price"] == 2.10


async def test_supplier_agent_never_sees_the_buyer_budget(tmp_path) -> None:
    orch, _ = orchestrator(tmp_path, [
        assessment(),
        AgentTurn(type="quote", text="EUR 2.10.", unit_price=2.10, currency="EUR", quantity=100000),
        AgentTurn(type="accept", text="Accepted."),
    ])
    await orch.handle_request(REQUEST, CLEAN_SUPPLIER)
    for name, _, payload in orch.runner.calls:
        side = "buyer" if name.startswith("buyer") else "supplier"
        assert ("budget" in payload["request"]) == (side == "buyer")
        assert ("budget" in payload["context"]["facts"]["tender"]) == (side == "buyer")


def test_deal_check_text_does_not_leak_the_budget_to_suppliers() -> None:
    from app.backend.knowledge.context import tender_facts

    tender = {"ref": "request:R", "title": "9x19 mm", "budget": 180000, "currency": "EUR", "deal_check": {
        "decision": "ESCALATE", "blocking": ["D14: value €180,000 > autonomous limit €250,000"],
        "conditions": ["D10: deal value 180000.00 vs revenue"], "checks": []}}

    supplier_view = json.dumps(tender_facts(tender, "supplier"))
    buyer_view = json.dumps(tender_facts(tender, "buyer"))

    assert "180,000" not in supplier_view and "180000" not in supplier_view
    assert "250,000" in supplier_view
    assert "180,000" in buyer_view
