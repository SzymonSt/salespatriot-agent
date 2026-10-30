"""Live checks against Supabase. Skipped unless DATABASE_URL is configured (env or app/backend/.env)."""

import pytest
from fastapi.testclient import TestClient

from app.backend.main import create_app
from app.backend.settings import Settings

pytestmark = pytest.mark.supabase


def live_settings() -> Settings:
    settings = Settings()
    if settings.async_database_url() is None:
        pytest.skip("DATABASE_URL is not configured")
    return settings


def test_context_for_a_live_open_tender() -> None:
    settings = live_settings()
    with TestClient(create_app(settings)) as client:
        orchestrator_client = client.app.state.knowledge_client
        tender = client.portal.call(orchestrator_client.pick_live_tender)
        if not tender:
            pytest.skip("No open TED tender with a family and a matched profiled supplier")
        response = client.post("/v1/context", json={
            "role": "supplier",
            "purpose": "assess_tender",
            "subject": {"publication_number": tender["publication_number"], "supplier_id": tender["supplier_id"]},
            "viewer_id": tender["supplier_id"],
        })

    if response.status_code == 503 and "not available in the database yet" in response.json()["detail"]:
        pytest.skip("agent_context is not deployed yet (WS3)")
    assert response.status_code == 200, response.text
    pack = response.json()
    for key in ("purpose", "subject", "facts", "candidates", "evidence", "price_anchors",
                "regulatory", "memory", "warnings", "refs"):
        assert key in pack
    for section in ("evidence", "price_anchors", "memory"):
        assert len(pack[section]) <= 8
        assert all("ref" in item for item in pack[section])


def test_family_schema_and_regulatory_profile_are_live() -> None:
    settings = live_settings()
    with TestClient(create_app(settings)) as client:
        knowledge = client.app.state.knowledge_client
        schema = client.portal.call(knowledge.family_schema, "ammunition.small_calibre")
        profile = client.portal.call(knowledge.regulatory_profile, "ammunition.small_calibre", "9x19")
    assert schema and "calibre" in schema["properties"]
    assert profile and "eu_ml" in profile
