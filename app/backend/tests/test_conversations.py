import pytest

from app.backend.agents.main import (
    BuyerMessageDraft,
    BuyerQuoteEvaluation,
    SupplierIntroductionDraft,
    SupplierQuoteDraft,
)
from app.backend.knowledge.conversations import (
    DEFAULT_MANDATE_SOURCE,
    ConversationAgentError,
    ConversationForbiddenError,
    ConversationOrchestrator,
    ConversationPolicyError,
    buyer_gate,
    check_supplier_quote,
    decide_on_quote,
    filter_refs,
)
from app.backend.knowledge.models import (
    CreateConversationRequest,
    Mandate,
    PostMessageRequest,
    RunLiveDemoRequest,
)
from app.backend.profiles import ProfileRegistry
from app.backend.settings import Settings
from app.backend.tests.knowledge_fakes import (
    AWARD_REF,
    BUYER,
    CONTRACT_REF,
    PUB,
    SUPPLIER,
    TENDER_REF,
    FakeEmbedder,
    FakeKnowledgeClient,
    ScriptedRunner,
    default_facts,
    default_pack,
)

INTRO = SupplierIntroductionDraft(
    non_binding_message="We delivered 9x19 lots before and would welcome a request.",
    caveats=["Availability needs confirmation"],
    refs=[CONTRACT_REF, "ted:invented-0000"],
)
RFQ = BuyerMessageDraft(action="request_quote", message="Please send an indicative quote.", refs=[TENDER_REF])


def quote(unit_price: float = 1.2, quantity: float = 50_000, currency: str = "EUR") -> SupplierQuoteDraft:
    return SupplierQuoteDraft(
        message=f"Indicative {unit_price} {currency} per round.",
        unit_price=unit_price,
        currency=currency,
        quantity=quantity,
        quantity_unit="rounds",
        quantity_is_indicative=True,
        lead_time_days=45,
        incoterms="DAP",
        conditions=["Subject to export licence"],
        refs=[CONTRACT_REF],
    )


def evaluation(recommendation: str, counter: float | None = None) -> BuyerQuoteEvaluation:
    return BuyerQuoteEvaluation(
        recommendation=recommendation,
        message=f"Buyer {recommendation}.",
        counter_unit_price=counter,
        rationale=["Compared with the same-family 9x19 anchor"],
        refs=[CONTRACT_REF],
    )


def orchestrator(client: FakeKnowledgeClient, runner: ScriptedRunner, **kwargs) -> ConversationOrchestrator:
    return ConversationOrchestrator(client=client, embedder=FakeEmbedder(), model="gemini-2.5-flash",
                                    run_agent=runner, **kwargs)


def full_request(**overrides) -> CreateConversationRequest:
    return CreateConversationRequest(publication_number=PUB, buyer_id=BUYER, supplier_id=SUPPLIER,
                                     demo=True, run="full", **overrides)


def types_of(client: FakeKnowledgeClient, conversation_id: str) -> list[str]:
    return [m["type"] for m in client.messages[conversation_id]]


# --------------------------------------------------------------------------- full runs


async def test_full_run_accepts_and_closes_with_outcome() -> None:
    client = FakeKnowledgeClient()

    async def quote_with_tool(agent, payload):
        tools = {tool.__name__: tool for tool in agent.tools}
        await tools["search_knowledge"]("9x19 awards", kinds=["award"])
        return quote()

    runner = ScriptedRunner({
        SupplierIntroductionDraft: [INTRO],
        BuyerMessageDraft: [RFQ],
        SupplierQuoteDraft: [quote_with_tool],
        BuyerQuoteEvaluation: [evaluation("accept")],
    })
    result = await orchestrator(client, runner).start(full_request())

    conversation_id = result["conversation_id"]
    messages = client.messages[conversation_id]
    assert types_of(client, conversation_id) == ["introduction", "check", "rfq", "quote", "accept"]
    assert [m["seq"] for m in messages] == [1, 2, 3, 4, 5]
    assert result["status"] == "closed"
    outcome = client.conversations[conversation_id]["outcome"]
    assert outcome["final_status"] == "accepted"
    assert outcome["final_price"] == 60_000
    assert outcome["demo"] is True
    assert client.outcomes[conversation_id]["unit_price"] == 1.2
    assert client.outcomes[conversation_id]["unit"] == "rounds"

    introduction = messages[0]
    assert introduction["refs"] == [CONTRACT_REF]
    assert introduction["payload"]["dropped_refs"] == ["ted:invented-0000"]
    assert introduction["model"] == "gemini-2.5-flash"
    assert messages[1]["sender_role"] == "system"
    assert messages[1]["payload"]["decision"] == "CLEARED_TO_REQUEST_QUOTE"
    assert messages[3]["payload"]["binding"] is False
    assert messages[3]["payload"]["total_price"] == 60_000

    logged_tools = [call["tool"] for call in client.tool_calls]
    assert logged_tools.count("agent_context") == 4
    assert "search_knowledge" in logged_tools
    search_call = next(c for c in client.tool_calls if c["tool"] == "search_knowledge")
    assert search_call["message_id"] == messages[3]["id"]
    assert search_call["result_refs"] == [CONTRACT_REF]
    search_args = next(kwargs for name, kwargs in client.calls if name == "search")
    assert search_args["viewer"] == SUPPLIER

    conversation = client.conversations[conversation_id]
    assert conversation["family_id"] == "ammunition.small_calibre"  # filled by the insert trigger
    assert conversation["buyer_mandate"]["source"] == DEFAULT_MANDATE_SOURCE
    assert result["conversation"]["messages"][-1]["type"] == "accept"


async def test_counter_round_then_accept() -> None:
    client = FakeKnowledgeClient()
    runner = ScriptedRunner({
        SupplierIntroductionDraft: [INTRO],
        BuyerMessageDraft: [RFQ],
        SupplierQuoteDraft: [quote(1.2), quote(1.1)],
        BuyerQuoteEvaluation: [evaluation("counter", 1.05), evaluation("accept")],
    })
    result = await orchestrator(client, runner).start(full_request())

    conversation_id = result["conversation_id"]
    assert types_of(client, conversation_id) == [
        "introduction", "check", "rfq", "quote", "counter", "quote", "accept"]
    counter = client.messages[conversation_id][4]
    assert counter["payload"]["counter_unit_price"] == 1.05
    assert counter["payload"]["round"] == 1
    second_quote_payload = runner.calls[4][2]
    assert second_quote_payload["buyer_message"]["type"] == "counter"
    assert second_quote_payload["previous_quote"]["unit_price"] == 1.2
    outcome = client.conversations[conversation_id]["outcome"]
    assert outcome["unit_price"] == 1.1
    assert outcome["rounds"] == 1


async def test_rounds_exhausted_escalates_without_closing() -> None:
    client = FakeKnowledgeClient()
    runner = ScriptedRunner({
        SupplierIntroductionDraft: [INTRO],
        BuyerMessageDraft: [RFQ],
        SupplierQuoteDraft: [quote(1.2)],
        BuyerQuoteEvaluation: [evaluation("counter", 1.0)],
    })
    result = await orchestrator(client, runner).start(full_request(buyer_mandate=Mandate(max_rounds=1)))

    conversation_id = result["conversation_id"]
    assert types_of(client, conversation_id) == [
        "introduction", "check", "rfq", "quote", "counter", "quote", "escalate"]
    escalation = client.messages[conversation_id][-1]
    assert escalation["payload"]["overridden_by_mandate"] is True
    assert "rounds exhausted" in escalation["payload"]["reasons"][0]
    assert result["status"] == "escalated"
    assert client.conversations[conversation_id]["closed_at"] is None


async def test_accept_above_autonomous_limit_is_escalated_by_mandate() -> None:
    client = FakeKnowledgeClient()
    runner = ScriptedRunner({
        SupplierIntroductionDraft: [INTRO],
        BuyerMessageDraft: [RFQ],
        SupplierQuoteDraft: [quote(1.2, 250_000)],
        BuyerQuoteEvaluation: [evaluation("accept")],
    })
    supplier_mandate = Mandate(max_autonomous_value_eur=1_000_000)
    result = await orchestrator(client, runner).start(full_request(supplier_mandate=supplier_mandate))

    last = client.messages[result["conversation_id"]][-1]
    assert last["type"] == "escalate"
    assert last["payload"]["model_recommendation"] == "accept"
    assert "autonomous limit" in last["payload"]["reasons"][0]


async def test_accept_over_price_ceiling_becomes_counter_at_ceiling() -> None:
    client = FakeKnowledgeClient()
    runner = ScriptedRunner({
        SupplierIntroductionDraft: [INTRO],
        BuyerMessageDraft: [RFQ],
        SupplierQuoteDraft: [quote(1.2), quote(1.0)],
        BuyerQuoteEvaluation: [evaluation("accept")],
    })
    mandate = Mandate(price_ceiling=50_000, currency="EUR")
    result = await orchestrator(client, runner).start(full_request(buyer_mandate=mandate))

    messages = client.messages[result["conversation_id"]]
    counter = messages[4]
    assert counter["type"] == "counter"
    assert counter["payload"]["counter_unit_price"] == 1.0
    assert counter["payload"]["overridden_by_mandate"] is True
    assert messages[-1]["type"] == "accept"
    assert result["status"] == "closed"


async def test_policy_gate_escalates_low_score_supplier_without_rfq() -> None:
    client = FakeKnowledgeClient(facts=default_facts(total_score=4.5))
    escalate = BuyerMessageDraft(action="escalate", message="A human needs to review this supplier.")
    runner = ScriptedRunner({SupplierIntroductionDraft: [INTRO], BuyerMessageDraft: [escalate]})
    result = await orchestrator(client, runner).start(full_request())

    conversation_id = result["conversation_id"]
    assert types_of(client, conversation_id) == ["introduction", "check", "escalate"]
    check = client.messages[conversation_id][1]
    assert check["payload"]["decision"] == "ESCALATE"
    assert runner.calls[1][2]["authorized_action"] == "escalate"
    assert "SupplierQuoteDraft" not in runner.models_called()


async def test_policy_gate_rejects_on_deal_check_no_go() -> None:
    facts = default_facts()
    facts["deal_check"] = {"decision": "NO-GO", "blocking": ["D1 sanctions"], "conditions": []}
    client = FakeKnowledgeClient(facts=facts)
    decline = BuyerMessageDraft(action="decline", message="We cannot proceed with this supplier.")
    runner = ScriptedRunner({SupplierIntroductionDraft: [INTRO], BuyerMessageDraft: [decline]})
    result = await orchestrator(client, runner).start(full_request())

    assert types_of(client, result["conversation_id"]) == ["introduction", "check", "reject"]
    assert result["status"] == "rejected"


async def test_buyer_agent_exceeding_authorized_action_escalates_and_raises() -> None:
    client = FakeKnowledgeClient()
    wrong = BuyerMessageDraft(action="decline", message="No.")
    runner = ScriptedRunner({SupplierIntroductionDraft: [INTRO], BuyerMessageDraft: [wrong]})

    with pytest.raises(ConversationAgentError, match="buyer-authorized action") as error:
        await orchestrator(client, runner).start(full_request())

    conversation_id = error.value.conversation_id
    assert types_of(client, conversation_id) == ["introduction", "check", "escalate"]
    assert client.messages[conversation_id][-1]["sender_role"] == "system"


async def test_invalid_or_out_of_mandate_quote_is_escalated_by_supplier() -> None:
    client = FakeKnowledgeClient()
    runner = ScriptedRunner({
        SupplierIntroductionDraft: [INTRO],
        BuyerMessageDraft: [RFQ],
        SupplierQuoteDraft: [quote(0.9)],
    })
    mandate = Mandate(min_unit_price=1.0, currency="EUR")
    result = await orchestrator(client, runner).start(full_request(supplier_mandate=mandate))

    last = client.messages[result["conversation_id"]][-1]
    assert last["type"] == "escalate"
    assert last["sender_role"] == "supplier"
    assert "supplier floor" in last["payload"]["reasons"][0]
    assert "BuyerQuoteEvaluation" not in runner.models_called()


async def test_signed_mandate_is_read_by_buyer_name_key() -> None:
    client = FakeKnowledgeClient(mandates={BUYER: {"company_id": BUYER, "max_autonomous_value_eur": 50_000,
                                                   "min_supplier_score": 7.0, "active": True}})
    runner = ScriptedRunner({SupplierIntroductionDraft: [INTRO]})
    result = await orchestrator(client, runner).start(
        CreateConversationRequest(publication_number=PUB, buyer_id=BUYER, supplier_id=SUPPLIER))

    mandate = client.conversations[result["conversation_id"]]["buyer_mandate"]
    assert mandate["source"] == "signed agent mandate"
    assert mandate["max_autonomous_value_eur"] == 50_000
    assert types_of(client, result["conversation_id"]) == ["introduction"]


async def test_non_demo_conversation_requires_opt_in() -> None:
    client = FakeKnowledgeClient()
    profiles = ProfileRegistry.load(Settings(_env_file=None).profiles_dir)
    with pytest.raises(ConversationPolicyError, match="not opted in"):
        await orchestrator(client, ScriptedRunner({}), profiles=profiles).start(
            CreateConversationRequest(publication_number=PUB, buyer_id=BUYER, supplier_id=SUPPLIER, demo=False))
    assert client.conversations == {}


async def test_run_live_demo_picks_tender_and_labels_simulation() -> None:
    client = FakeKnowledgeClient(live_tender={"publication_number": PUB, "buyer_name": "Generálne riaditeľstvo",
                                              "buyer_id": None, "supplier_id": SUPPLIER})
    runner = ScriptedRunner({
        SupplierIntroductionDraft: [INTRO], BuyerMessageDraft: [RFQ],
        SupplierQuoteDraft: [quote()], BuyerQuoteEvaluation: [evaluation("accept")],
    })
    result = await orchestrator(client, runner).run_live_demo(RunLiveDemoRequest())

    assert result["buyer_is_simulated"] is True
    assert result["demo"] is True
    assert result["status"] == "closed"
    assert client.conversations[result["conversation_id"]]["buyer_id"] == "buyer:Generálne riaditeľstvo"


# --------------------------------------------------------------------------- external messages


async def test_posted_messages_run_the_counterpart_turn() -> None:
    client = FakeKnowledgeClient()
    runner = ScriptedRunner({
        SupplierIntroductionDraft: [INTRO],
        SupplierQuoteDraft: [quote()],
        BuyerQuoteEvaluation: [evaluation("accept")],
    })
    engine = orchestrator(client, runner)
    started = await engine.start(CreateConversationRequest(publication_number=PUB, buyer_id=BUYER,
                                                           supplier_id=SUPPLIER))
    conversation_id = started["conversation_id"]

    with pytest.raises(ConversationForbiddenError):
        await engine.post_message(conversation_id, PostMessageRequest(
            sender_role="buyer", sender_id="someone-else", type="rfq", text="Quote please"))

    result = await engine.post_message(conversation_id, PostMessageRequest(
        sender_role="buyer", sender_id=BUYER, type="rfq", text="Quote please", refs=[TENDER_REF]))
    assert types_of(client, conversation_id) == ["introduction", "rfq", "quote"]
    assert result["status"] == "quoted"

    result = await engine.post_message(conversation_id, PostMessageRequest(
        sender_role="buyer", sender_id=BUYER, type="accept", text="Accepted"))
    assert types_of(client, conversation_id) == ["introduction", "rfq", "quote", "accept"]
    assert result["status"] == "closed"
    assert client.conversations[conversation_id]["outcome"]["accepted_by"] == "buyer"


async def test_posted_supplier_quote_runs_buyer_evaluation() -> None:
    client = FakeKnowledgeClient()
    runner = ScriptedRunner({SupplierIntroductionDraft: [INTRO], BuyerQuoteEvaluation: [evaluation("accept")]})
    engine = orchestrator(client, runner)
    conversation_id = (await engine.start(CreateConversationRequest(
        publication_number=PUB, buyer_id=BUYER, supplier_id=SUPPLIER)))["conversation_id"]
    await client.append_message(conversation_id, {"sender_role": "buyer", "sender_id": BUYER, "type": "rfq"})

    with pytest.raises(ConversationPolicyError, match="unit_price"):
        await engine.post_message(conversation_id, PostMessageRequest(
            sender_role="supplier", sender_id=SUPPLIER, type="quote", text="Cheap", payload={}))

    await engine.post_message(conversation_id, PostMessageRequest(
        sender_role="supplier", sender_id=SUPPLIER, type="quote", text="1.2 EUR",
        payload={"unit_price": 1.2, "quantity": 1000, "currency": "eur"}))
    assert types_of(client, conversation_id)[-1] == "accept"
    evaluation_payload = runner.calls[-1][2]
    assert evaluation_payload["quote"]["total_price"] == 1200
    assert all(m["payload"].get("unit_price") for m in client.messages[conversation_id] if m["type"] == "quote")


async def test_supplier_accepting_a_counter_closes_at_the_counter_price() -> None:
    client = FakeKnowledgeClient()
    runner = ScriptedRunner({SupplierIntroductionDraft: [INTRO], SupplierQuoteDraft: [quote(1.2, 1000)]})
    engine = orchestrator(client, runner)
    conversation_id = (await engine.start(CreateConversationRequest(
        publication_number=PUB, buyer_id=BUYER, supplier_id=SUPPLIER)))["conversation_id"]
    await engine.post_message(conversation_id, PostMessageRequest(
        sender_role="buyer", sender_id=BUYER, type="rfq", text="Quote?"))
    await client.append_message(conversation_id, {
        "sender_role": "buyer", "sender_id": BUYER, "type": "counter",
        "payload": {"counter_unit_price": 1.0, "currency": "EUR", "quantity": 1000}})

    result = await engine.post_message(conversation_id, PostMessageRequest(
        sender_role="supplier", sender_id=SUPPLIER, type="accept", text="Agreed"))

    assert result["status"] == "closed"
    assert client.outcomes[conversation_id]["final_price"] == 1000
    assert client.outcomes[conversation_id]["unit_price"] == 1.0


# --------------------------------------------------------------------------- pure policy functions


def test_buyer_gate_needs_known_score_kyc_and_evidence() -> None:
    pack = default_pack("find_suppliers", {"publication_number": PUB})
    cleared = buyer_gate(supplier_id=SUPPLIER, mandate=Mandate(source=DEFAULT_MANDATE_SOURCE),
                         pack=pack, facts=default_facts())
    assert cleared.decision == "CLEARED_TO_REQUEST_QUOTE"
    assert "Both parties sign their agent mandate (K11)" in cleared.conditions

    unknown = default_facts(total_score=None, kyc_status=None)
    gate = buyer_gate(supplier_id=SUPPLIER, mandate=Mandate(), pack={**pack, "evidence": []}, facts=unknown)
    assert gate.decision == "ESCALATE"
    assert {c["code"] for c in gate.checks if c["status"] == "escalate"} == {"dd_score", "kyc", "evidence"}

    other = buyer_gate(supplier_id="supplier-other", mandate=Mandate(), pack=pack, facts=default_facts())
    assert other.decision == "ESCALATE"


def test_buyer_gate_value_rules_follow_d14() -> None:
    pack = default_pack("find_suppliers", {"publication_number": PUB})
    facts = default_facts()
    facts["tender"]["estimated_value"] = 2_982_560
    assert buyer_gate(supplier_id=SUPPLIER, mandate=Mandate(), pack=pack, facts=facts).decision == "ESCALATE"
    facts["tender"]["estimated_value"] = None
    gate = buyer_gate(supplier_id=SUPPLIER, mandate=Mandate(), pack=pack, facts=facts)
    assert gate.decision == "CLEARED_TO_REQUEST_QUOTE"
    assert any("Accept only an offer" in condition for condition in gate.conditions)


def test_quote_checks_reject_non_eur_totals_and_bad_numbers() -> None:
    assert check_supplier_quote(quote(currency="CZK"), Mandate()).problems
    assert check_supplier_quote(quote(unit_price=0), Mandate()).quote is None
    ok = check_supplier_quote(quote(), Mandate())
    assert ok.problems == []
    assert ok.quote["total_price"] == 60_000


def test_decide_on_quote_requires_cited_evidence_and_valid_counter() -> None:
    payload = check_supplier_quote(quote(), Mandate()).quote
    no_refs = decide_on_quote(evaluation=evaluation("accept"), quote=payload,
                              mandate=Mandate(min_evidence_refs=1), rounds_used=0, cited_refs=[])
    assert no_refs.action == "escalate"
    higher = decide_on_quote(evaluation=evaluation("counter", 1.5), quote=payload,
                             mandate=Mandate(), rounds_used=0, cited_refs=[CONTRACT_REF])
    assert higher.action == "escalate"


def test_filter_refs_keeps_only_known_refs_once() -> None:
    kept, dropped = filter_refs([AWARD_REF, AWARD_REF, "invented", " "], {AWARD_REF})
    assert kept == [AWARD_REF]
    assert dropped == ["invented"]
