import uuid

import pytest
from fastapi.testclient import TestClient

from app.backend.agents.main import (
    BuyerMessageDraft,
    BuyerQuoteEvaluation,
    SupplierIntroductionDraft,
    SupplierQuoteDraft,
)
from app.backend.knowledge.client import KnowledgeRejectedError, KnowledgeUnavailableError
from app.backend.knowledge.routes import get_knowledge_client, get_query_embedder
from app.backend.main import create_app
from app.backend.settings import Settings
from app.backend.tests.knowledge_fakes import (
    BUYER,
    CONTRACT_REF,
    PUB,
    SUPPLIER,
    FakeEmbedder,
    FakeKnowledgeClient,
    ScriptedRunner,
)

CONTEXT_BODY = {
    "role": "supplier",
    "purpose": "assess_tender",
    "subject": {"publication_number": PUB, "supplier_id": SUPPLIER},
    "query": "9x19 price anchors",
    "viewer_id": SUPPLIER,
}


def make_client(fake: FakeKnowledgeClient | None = None, *, google_api_key: str | None = "not-a-real-key",
                embedder: FakeEmbedder | None = None) -> TestClient:
    app = create_app(Settings(_env_file=None, google_api_key=google_api_key))
    if fake is not None:
        app.dependency_overrides[get_knowledge_client] = lambda: fake
        app.dependency_overrides[get_query_embedder] = lambda: embedder or FakeEmbedder()
    return TestClient(app)


def test_routes_return_503_without_database_url() -> None:
    conversation_id = str(uuid.uuid4())
    requests = [
        ("post", "/v1/context", CONTEXT_BODY),
        ("post", "/v1/knowledge/search", {"query": "9x19", "viewer_id": SUPPLIER}),
        ("post", "/v1/memory/recall", {"query": "x", "role": "buyer", "viewer_id": BUYER}),
        ("get", f"/v1/conversations/{conversation_id}", None),
        ("post", "/api/demo/run-live", None),
    ]
    with make_client() as client:
        for method, path, body in requests:
            response = client.request(method, path, json=body)
            assert response.status_code == 503, path
            assert "DATABASE_URL" in response.json()["detail"]


def test_context_embeds_query_and_returns_pack() -> None:
    fake = FakeKnowledgeClient()
    embedder = FakeEmbedder()
    with make_client(fake, embedder=embedder) as client:
        response = client.post("/v1/context", json=CONTEXT_BODY)

    assert response.status_code == 200
    assert response.headers["X-Query-Embedding"] == "used"
    assert response.json()["refs"][0] == f"ted:{PUB}"
    _, call = fake.calls[-1]
    assert call["query_embedding"] == embedder.vector
    assert call["query_text"] == "9x19 price anchors"
    assert call["viewer"] == SUPPLIER
    assert call["limit"] == 8


@pytest.mark.parametrize(
    "body",
    [
        {**CONTEXT_BODY, "subject": {"publication_number": PUB}},
        {**CONTEXT_BODY, "subject": {**CONTEXT_BODY["subject"], "sql": "drop"}},
        {**CONTEXT_BODY, "purpose": "search", "query": None},
        {**CONTEXT_BODY, "purpose": "everything"},
        {**CONTEXT_BODY, "unexpected": True},
    ],
)
def test_context_rejects_invalid_requests(body: dict) -> None:
    with make_client(FakeKnowledgeClient()) as client:
        assert client.post("/v1/context", json=body).status_code == 422


def test_database_errors_map_to_http_status(monkeypatch) -> None:
    fake = FakeKnowledgeClient()

    async def unavailable(**kwargs):
        raise KnowledgeUnavailableError("agent_context is not available in the database yet (x)")

    async def rejected(*args, **kwargs):
        raise KnowledgeRejectedError("agent_message_append was rejected: accept needs a quote")

    monkeypatch.setattr(fake, "context", unavailable)
    with make_client(fake) as client:
        response = client.post("/v1/context", json=CONTEXT_BODY)
    assert response.status_code == 503
    assert "not available" in response.json()["detail"]

    fake = FakeKnowledgeClient()
    conversation_id = str(uuid.uuid4())
    fake.conversations[conversation_id] = {"id": conversation_id, "status": "open", "closed_at": None,
                                           "buyer_id": BUYER, "supplier_id": SUPPLIER, "publication_number": PUB}
    fake.messages[conversation_id] = []
    monkeypatch.setattr(fake, "append_message", rejected)
    with make_client(fake) as client:
        response = client.post(f"/v1/conversations/{conversation_id}/messages",
                               json={"sender_role": "buyer", "sender_id": BUYER, "type": "accept"})
    assert response.status_code == 409


def test_knowledge_search_and_memory_recall() -> None:
    fake = FakeKnowledgeClient()
    with make_client(fake) as client:
        search = client.post("/v1/knowledge/search", json={
            "query": "cartridges 9x19", "kinds": ["award", "contract"], "cpv_prefix": "3533",
            "country": "POL", "viewer_id": BUYER, "limit": 5})
        bad_kind = client.post("/v1/knowledge/search", json={"query": "x", "kinds": ["invoice"],
                                                               "viewer_id": BUYER})
        recall = client.post("/v1/memory/recall", json={"query": "late deliveries", "role": "buyer",
                                                         "viewer_id": BUYER})

    assert search.status_code == 200
    assert search.json()["hits"][0]["ref"] == CONTRACT_REF
    assert bad_kind.status_code == 422
    assert recall.json()["memories"][0]["ref"].startswith("memory:")
    _, recall_call = fake.calls[-1]
    assert recall_call["role"] == "buyer"
    assert recall_call["viewer"] == BUYER


def test_memory_write_allows_only_proposed_private_entries() -> None:
    fake = FakeKnowledgeClient()
    entry = {"kind": "preference", "role": "buyer", "owner_id": BUYER, "text": "Prefers DAP delivery",
             "visibility": "parties", "party_ids": [SUPPLIER]}
    with make_client(fake) as client:
        created = client.post("/v1/memory", json=entry)
        lesson = client.post("/v1/memory", json={**entry, "kind": "lesson"})
        validated = client.post("/v1/memory", json={**entry, "status": "validated"})
        public = client.post("/v1/memory", json={**entry, "visibility": "public"})
        episode = client.post("/v1/memory", json={**entry, "kind": "episode"})
        memory_id = created.json()["id"]
        feedback = client.post(f"/v1/memory/{memory_id}/feedback", json={"helpful": True})
        missing = client.post(f"/v1/memory/{uuid.uuid4()}/feedback", json={"helpful": False})

    assert created.status_code == 201
    assert created.json()["ref"] == f"memory:{memory_id}"
    written = fake.calls[0][1]["entry"]
    assert written["status"] == "proposed"
    assert written["writer"] == "agent"
    assert written["party_ids"] == [SUPPLIER, BUYER]
    assert [lesson.status_code, validated.status_code, public.status_code, episode.status_code] == [422] * 4
    assert feedback.json() == {"ref": f"memory:{memory_id}", "helpful": 1, "unhelpful": 0}
    assert missing.status_code == 404


def test_conversation_routes(monkeypatch) -> None:
    fake = FakeKnowledgeClient()
    runner = ScriptedRunner({
        SupplierIntroductionDraft: [SupplierIntroductionDraft(non_binding_message="Hello", refs=[CONTRACT_REF])],
        SupplierQuoteDraft: [SupplierQuoteDraft(
            message="Indicative", unit_price=1.2, currency="EUR", quantity=1000, quantity_unit="rounds",
            quantity_is_indicative=True, lead_time_days=30, refs=[CONTRACT_REF])],
    })
    monkeypatch.setattr("app.backend.knowledge.conversations.run_structured_agent", runner)
    with make_client(fake) as client:
        created = client.post("/v1/conversations", json={
            "publication_number": PUB, "buyer_id": BUYER, "supplier_id": SUPPLIER, "demo": True})
        conversation_id = created.json()["conversation_id"]
        wrong_type = client.post(f"/v1/conversations/{conversation_id}/messages",
                                 json={"sender_role": "system", "sender_id": "x", "type": "quote"})
        rfq = client.post(f"/v1/conversations/{conversation_id}/messages",
                          json={"sender_role": "buyer", "sender_id": BUYER, "type": "rfq", "text": "Quote?"})
        replay = client.get(f"/v1/conversations/{conversation_id}")
        outcome = client.post(f"/v1/conversations/{conversation_id}/outcome", json={
            "delivered_on_time": True, "final_price": 1200, "currency": "EUR", "reported_by": BUYER})
        missing = client.get(f"/v1/conversations/{uuid.uuid4()}")
        not_uuid = client.get("/v1/conversations/not-a-uuid")

    assert created.status_code == 201
    assert created.json()["conversation"]["messages"][0]["type"] == "introduction"
    assert wrong_type.status_code == 422
    assert rfq.status_code == 200
    assert [m["type"] for m in replay.json()["messages"]] == ["introduction", "rfq", "quote"]
    assert replay.json()["tool_calls"]
    assert outcome.status_code == 200
    assert outcome.json()["outcome"]["final_price"] == 1200
    assert outcome.json()["close"]["final_status"] == "quoted"
    assert fake.conversations[conversation_id]["closed_at"] is not None
    assert missing.status_code == 404
    assert not_uuid.status_code == 422


def test_agent_routes_require_gemini_key() -> None:
    with make_client(FakeKnowledgeClient(), google_api_key=None) as client:
        response = client.post("/v1/conversations", json={
            "publication_number": PUB, "buyer_id": BUYER, "supplier_id": SUPPLIER})
        live = client.post("/api/demo/run-live")
    assert response.status_code == 503
    assert "GOOGLE_API_KEY" in response.json()["detail"]
    assert live.status_code == 503


def test_run_live_demo_persists_a_closed_conversation(monkeypatch) -> None:
    fake = FakeKnowledgeClient(live_tender={"publication_number": PUB, "buyer_name": "Zbor väzenskej stráže",
                                            "buyer_id": None, "supplier_id": SUPPLIER})
    runner = ScriptedRunner({
        SupplierIntroductionDraft: [SupplierIntroductionDraft(non_binding_message="Hello", refs=[CONTRACT_REF])],
        BuyerMessageDraft: [BuyerMessageDraft(action="request_quote", message="Quote please")],
        SupplierQuoteDraft: [SupplierQuoteDraft(
            message="Indicative", unit_price=1.2, currency="EUR", quantity=1000, quantity_unit="rounds",
            quantity_is_indicative=True, lead_time_days=30, refs=[CONTRACT_REF])],
        BuyerQuoteEvaluation: [BuyerQuoteEvaluation(recommendation="accept", message="Accepted",
                                                    refs=[CONTRACT_REF])],
    })
    monkeypatch.setattr("app.backend.knowledge.conversations.run_structured_agent", runner)
    with make_client(fake) as client:
        response = client.post("/api/demo/run-live", json={"supplier_id": SUPPLIER})
        empty = make_client(FakeKnowledgeClient())
        with empty as other:
            not_found = other.post("/api/demo/run-live")

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "closed"
    assert result["demo"] is True
    assert result["buyer_is_simulated"] is True
    messages = result["conversation"]["messages"]
    assert [m["type"] for m in messages] == ["introduction", "check", "rfq", "quote", "accept"]
    assert all(m["refs"] for m in messages if m["sender_role"] != "system" and m["type"] != "rfq")
    assert not_found.status_code == 404
