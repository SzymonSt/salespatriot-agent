from google.adk.tools import FunctionTool

from app.backend.agents.main import (
    KNOWLEDGE_INSTRUCTION,
    SupplierIntroductionDraft,
    build_buyer_agent,
    build_supplier_agent,
)
from app.backend.knowledge.tools import (
    KnowledgeToolContext,
    ToolCallRecorder,
    build_knowledge_tools,
    collect_refs,
)
from app.backend.tests.knowledge_fakes import (
    CONTRACT_REF,
    PUB,
    SUPPLIER,
    TENDER_REF,
    FakeEmbedder,
    FakeKnowledgeClient,
)


def tools_for(role: str, viewer: str, conversation_id: str | None = None):
    client = FakeKnowledgeClient()
    embedder = FakeEmbedder()
    recorder = ToolCallRecorder()
    tools = build_knowledge_tools(KnowledgeToolContext(
        client=client, embedder=embedder, role=role, viewer_id=viewer,
        conversation_id=conversation_id, recorder=recorder))
    return {tool.__name__: tool for tool in tools}, client, embedder, recorder


async def test_assess_tender_always_uses_the_bound_supplier() -> None:
    tools, client, _, recorder = tools_for("supplier", SUPPLIER)

    pack = await tools["get_context"]("assess_tender", publication_number=PUB, supplier_id="supplier-other")

    _, call = client.calls[-1]
    assert call["subject"] == {"publication_number": PUB, "supplier_id": SUPPLIER}
    assert call["viewer"] == SUPPLIER
    assert call["role"] == "supplier"
    assert pack["purpose"] == "assess_tender"
    assert recorder.calls[0].result_refs == [TENDER_REF, CONTRACT_REF]


async def test_purposes_are_limited_by_role_and_conversation() -> None:
    tools, client, _, recorder = tools_for("supplier", SUPPLIER)
    result = await tools["get_context"]("find_suppliers", publication_number=PUB)
    assert "not available to a supplier agent" in result["error"]
    assert recorder.calls[-1].error is not None
    assert client.calls == []

    buyer_tools, buyer_client, _, _ = tools_for("buyer", "buyer-1")
    result = await buyer_tools["get_context"]("evaluate_quote", message_id="m-1")
    assert "only available inside a conversation" in result["error"]

    bound_tools, bound_client, _, _ = tools_for("buyer", "buyer-1", conversation_id="c-1")
    await bound_tools["get_context"]("evaluate_quote", message_id="m-1")
    assert bound_client.calls[-1][1]["subject"] == {"message_id": "m-1", "conversation_id": "c-1"}


async def test_search_and_recall_embed_the_query_and_bind_the_viewer() -> None:
    tools, client, embedder, recorder = tools_for("buyer", "buyer-1")

    result = await tools["search_knowledge"]("9x19 ammunition", kinds=["award", "contract"], country="pol")
    assert result["hits"][0]["ref"] == CONTRACT_REF
    _, search = client.calls[-1]
    assert search["viewer"] == "buyer-1"
    assert search["country"] == "POL"
    assert search["query_embedding"] == embedder.vector

    bad = await tools["search_knowledge"]("x", kinds=["invoice"])
    assert "unknown kinds" in bad["error"]

    memories = await tools["recall_memory"]("late deliveries", family_id="ammunition.small_calibre")
    assert memories["memories"][0]["ref"].startswith("memory:")
    _, recall = client.calls[-1]
    assert recall["role"] == "buyer"
    assert recall["viewer"] == "buyer-1"
    assert [call.tool for call in recorder.calls] == ["search_knowledge", "search_knowledge", "recall_memory"]


async def test_family_schema_and_regulatory_profile_tools() -> None:
    tools, client, _, _ = tools_for("supplier", SUPPLIER)

    schema = await tools["get_family_schema"]("ammunition.small_calibre")
    assert schema["schema"]["required"] == ["calibre"]
    missing = await tools["get_family_schema"]("unknown.family")
    assert "unknown product family" in missing["error"]

    profile = await tools["regulatory_profile"]("ammunition.small_calibre", "9x19")
    assert "not a licence decision" in profile["note"]
    assert client.calls[-1] == ("regulatory_profile", {"family_id": "ammunition.small_calibre", "calibre": "9x19"})


def test_tool_declarations_are_valid_for_gemini() -> None:
    tools, _, _, _ = tools_for("buyer", "buyer-1")
    declarations = {name: FunctionTool(tool)._get_declaration() for name, tool in tools.items()}

    assert set(declarations) == {
        "get_context", "search_knowledge", "recall_memory", "get_family_schema", "regulatory_profile"}
    for declaration in declarations.values():
        properties = declaration.parameters.properties
        assert "viewer" not in properties and "viewer_id" not in properties and "role" not in properties
    assert declarations["get_context"].parameters.required == ["purpose"]


def test_agents_accept_tools_together_with_output_schema() -> None:
    tools, _, _, _ = tools_for("supplier", SUPPLIER)
    agent = build_supplier_agent(SUPPLIER, "gemini-2.5-flash", tools=list(tools.values()))

    assert agent.output_schema is SupplierIntroductionDraft
    assert len(agent.tools) == 5
    assert KNOWLEDGE_INSTRUCTION in agent.instruction

    plain = build_buyer_agent("buyer-1", "gemini-2.5-flash")
    assert plain.tools == []
    assert KNOWLEDGE_INSTRUCTION not in plain.instruction


def test_collect_refs_walks_packs() -> None:
    pack = {"facts": {"tender": {"ref": "ted:1"}}, "evidence": [{"ref": "contract:a"}],
            "refs": ["ted:1", "award:b"], "memory": [{"evidence_refs": ["conv:c"]}]}
    assert collect_refs(pack) == ["ted:1", "contract:a", "award:b", "conv:c"]
