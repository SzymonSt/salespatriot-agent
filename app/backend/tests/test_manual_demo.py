from fastapi.testclient import TestClient

from app.backend.agents.main import BuyerMessageDraft, SupplierIntroductionDraft
from app.backend.main import create_app
from app.backend.settings import Settings


def test_manual_demo_page_and_synthetic_flow(monkeypatch) -> None:
    async def fake_agent_run(agent, output_model, user_id, payload):
        if output_model is SupplierIntroductionDraft:
            return SupplierIntroductionDraft(
                non_binding_message="We are interested in discussing this demo demand.",
                caveats=["Illustrative only"],
            )
        return BuyerMessageDraft(
            action="request_quote",
            message="Please provide an indicative quote for the demo demand.",
        )

    monkeypatch.setattr("app.backend.demo.run_structured_agent", fake_agent_run)
    app = create_app(
        Settings(
            _env_file=None,
            google_api_key="not-a-real-key",
        )
    )

    with TestClient(app) as client:
        page = client.get("/demo")
        response = client.post("/api/demo/run")

    assert page.status_code == 200
    assert "Run demo" in page.text
    assert response.status_code == 200
    result = response.json()
    assert result["demo_only"] is True
    assert result["persisted"] is False
    assert result["external_messages_sent"] is False
    stages = [step["stage"] for step in result["timeline"]]
    assert stages.index("supplier_agent_introduction") < stages.index(
        "supplier_reference_request"
    )
    assert stages[-1] == "quote_comparison"
    assert result["selected_quote"]["supplier_id"] == "demo-supplier-east"
