from fastapi.testclient import TestClient

from app.backend.main import create_app
from app.backend.profiles import ProfileRegistry
from app.backend.settings import Settings


def make_client() -> TestClient:
    return TestClient(create_app(Settings(_env_file=None)))


def test_health_and_profile_summary() -> None:
    with make_client() as client:
        assert client.get("/health/live").status_code == 200
        response = client.get("/v1/profiles/summary")

    profiles = ProfileRegistry.load(Settings(_env_file=None).profiles_dir)
    assert response.status_code == 200
    assert response.json() == {
        "buyers": len(profiles.buyers),
        "suppliers": len(profiles.suppliers),
    }


def test_readiness_is_not_successful_without_database_url() -> None:
    with make_client() as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert "DATABASE_URL" in response.json()["detail"]


def test_readiness_checks_a_configured_async_database() -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///:memory:",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "connected"}


def test_supplier_preview_requires_explicit_opt_in() -> None:
    with make_client() as client:
        response = client.post(
            "/v1/agents/suppliers/supplier-cze-65414128/preview",
            json={
                "demand_id": "notice-1",
                "title": "Example demand",
                "cpv_codes": ["35300000"],
            },
        )

    assert response.status_code == 409
    assert "not opted in" in response.json()["detail"]


def test_buyer_message_requires_active_policy() -> None:
    with make_client() as client:
        response = client.post(
            "/v1/agents/buyers/buyer-deu-991-19518-88/draft-message",
            json={
                "action": "request_quote",
                "decision": "GO",
                "evidence_summary": "Checks completed",
            },
        )

    assert response.status_code == 409
    assert "no active" in response.json()["detail"]
