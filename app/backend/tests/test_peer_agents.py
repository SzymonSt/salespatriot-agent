from fastapi.testclient import TestClient

from app.backend.main import create_app
from app.backend.profiles import CompanyProfile, ProfileRegistry
from app.backend.settings import Settings
from app.backend.agents.peer import BuyerAgentDirectory


def test_actual_profile_agents_exchange_public_counterpart_evidence() -> None:
    app = create_app(Settings(_env_file=None))

    with TestClient(app) as client:
        options = client.get("/v1/agents/reference-options").json()
        response = client.post(
            "/v1/agents/buyers/buyer-aut-9110010282738/reference-requests",
            json={
                "supplier_id": "supplier-svn-2237741000",
                "peer_buyer_id": "buyer-svn-5268923000",
            },
        )

    assert response.status_code == 200
    result = response.json()
    assert result["request"]["sender_agent_id"] == (
        "buyer-agent-buyer-aut-9110010282738"
    )
    assert result["request"]["recipient_agent_id"] == (
        "buyer-agent-buyer-svn-5268923000"
    )
    assert result["response"]["sender_agent_id"] == (
        "buyer-agent-buyer-svn-5268923000"
    )
    assert result["response"]["evidence"] == [
        {
            "source": "buyer_profile_top_counterparts",
            "counterpart_notice_count": 2,
            "notice_number": None,
            "title": None,
            "published": None,
            "ted_url": None,
            "cpv_codes": [],
        }
    ]
    assert result["response"]["performance_assessment"] == (
        "not_available_in_profile"
    )
    assert result["private_profile_data_shared"] is False
    assert result["external_organization_contacted"] is False
    assert result["persisted"] is False
    assert {
        (supplier["supplier_id"], tuple(supplier["peer_buyer_ids"]))
        for supplier in options["suppliers"]
    } == {
        ("supplier-svn-2237741000", ("buyer-svn-5268923000",)),
        ("supplier-est-11535997", ("buyer-ltu-188729923",)),
    }


def test_peer_agent_never_serializes_private_profile_fields() -> None:
    requester = CompanyProfile(
        id="buyer-a",
        role="demand",
        name="Buyer A",
        country="AAA",
        agent_policy={"private_note": "requester-secret"},
    )
    peer = CompanyProfile(
        id="buyer-b",
        role="demand",
        name="Buyer B",
        country="BBB",
        top_counterparts=[{"name": "Supplier X", "notices": 1}],
        kyc={"private_note": "peer-secret"},
    )
    supplier = CompanyProfile(
        id="supplier-x",
        role="supply",
        name="Supplier X",
        country="XXX",
        due_diligence={"private_note": "supplier-secret"},
    )
    directory = BuyerAgentDirectory(
        ProfileRegistry(
            buyers={"buyer-a": requester, "buyer-b": peer},
            suppliers={"supplier-x": supplier},
        )
    )

    result = directory.run_reference_check("buyer-a", "supplier-x", "buyer-b")
    serialized = result.model_dump_json()

    assert "requester-secret" not in serialized
    assert "peer-secret" not in serialized
    assert "supplier-secret" not in serialized
    assert result.response.status == "public_relationship_found"
    assert "not an endorsement" in result.response.summary
