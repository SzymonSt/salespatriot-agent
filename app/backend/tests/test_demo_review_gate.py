"""Demo flow: price list as the price basis, and the buyer's review gate (no Gemini calls)."""

import asyncio

from app.backend.agents.company import AgentTurn
from app.backend.live import ConversationOrchestrator, SimulatedKnowledgeClient
from app.backend.live.conversations import price_list_entry
from app.backend.tests.test_company_agents import BUYER, PROFILES, assessment, fake_runner

LUCZNIK = "supplier-pol-9482182612"
GROT_REQUEST = {"id": "REQ-GROT", "buyer_id": BUYER, "title": "Spare parts for 5.56 mm MSBS GROT carbines",
                "cpv_codes": ["35340000"], "quantity": 400, "unit": "sets", "budget": 200000, "currency": "EUR"}


def gated(tmp_path, script):
    client = SimulatedKnowledgeClient(tmp_path / "agent_db.json", PROFILES)
    orch = ConversationOrchestrator(client, PROFILES, "fake-model", runner=fake_runner(script), review_gate=True)
    return orch, client


def test_price_list_matches_only_when_all_keywords_appear() -> None:
    assert price_list_entry(LUCZNIK, GROT_REQUEST)["unit_price"] == 430
    assert price_list_entry(LUCZNIK, {"title": "MSBS GROT rifles"}) is None
    assert price_list_entry("supplier-ita-n0057381f", GROT_REQUEST) is None


async def test_review_gate_pauses_after_the_supplier_opening(tmp_path) -> None:
    orch, client = gated(tmp_path, [assessment(), AgentTurn(type="introduction", text="We make GROT spare parts.")])
    await orch.handle_request(GROT_REQUEST, LUCZNIK)
    [conversation] = await client.list_conversations(BUYER)

    assert conversation["status"] == "awaiting_review"
    assert [m["type"] for m in conversation["messages"]] == ["check", "introduction"]
    # The price list gives the supplier a floor of list price -3%.
    assert conversation["rules"]["min_unit_price"] == round(430 * 0.97, 4)
    assert "price_list" in conversation["supplier_mandate"]


async def test_resume_lets_the_agents_close_the_deal(tmp_path) -> None:
    orch, client = gated(tmp_path, [
        assessment(),
        AgentTurn(type="introduction", text="We make GROT spare parts."),
        AgentTurn(type="rfq", text="Please quote 400 sets."),
        AgentTurn(type="quote", text="EUR 430 per set.", unit_price=430, currency="EUR", quantity=400, lead_time_days=98),
        AgentTurn(type="accept", text="Accepted."),
    ])
    await orch.handle_request(GROT_REQUEST, LUCZNIK)
    [conversation] = await client.list_conversations(BUYER)
    await orch.resume(conversation["id"])
    await asyncio.gather(*orch.tasks)
    [conversation] = await client.list_conversations(BUYER)

    assert conversation["status"] == "accepted"
    assert conversation["outcome"]["total"] == 172000
    # check (assessment), introduction, check (review), check (buyer's due diligence), rfq, quote, accept
    assert [m["type"] for m in conversation["messages"]] == ["check", "introduction", "check", "check", "rfq", "quote", "accept"]
    assert conversation["messages"][3]["payload"]["buyer_checks"]["due_diligence"]["score"] == 7.2


async def test_agents_agree_then_a_person_approves(tmp_path) -> None:
    client = SimulatedKnowledgeClient(tmp_path / "agent_db.json", PROFILES)
    runner = fake_runner([
        assessment(),
        AgentTurn(type="quote", text="EUR 430 per set.", unit_price=430, currency="EUR", quantity=400, lead_time_days=98),
        AgentTurn(type="question", text="Which transfer licence applies?"),
        AgentTurn(type="answer", text="A Polish general transfer licence."),
        AgentTurn(type="accept", text="Accepted."),
    ])
    orch = ConversationOrchestrator(client, PROFILES, "fake-model", runner=runner, clarify_first=True)
    await orch.handle_request(GROT_REQUEST, LUCZNIK)
    [conversation] = await client.list_conversations(BUYER)
    assert conversation["status"] == "accepted" and "approval" not in conversation
    # The buyer's first turn was told to ask about the deal check first.
    buyer_payloads = [payload for name, model, payload in runner.calls if model == "AgentTurn" and "other_offers" in payload]
    assert "instruction" in buyer_payloads[0] and "instruction" not in buyer_payloads[1]

    approved = await orch.approve(conversation["id"], "approved", "BAAINBw procurement officer")
    assert approved["approval"]["decision"] == "approved"
    assert approved["messages"][-1]["text"].startswith("Deal approved")
