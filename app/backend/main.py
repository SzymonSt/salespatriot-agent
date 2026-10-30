from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from app.backend.agents.main import (
    BuyerMessageDraft,
    SupplierIntroductionDraft,
    build_buyer_agent,
    build_supplier_agent,
)
from app.backend.agents.runtime import (
    AgentRuntimeError,
    configure_gemini,
    run_structured_agent,
)
from app.backend.database import check_database_connection, create_database_engine
from app.backend.knowledge.client import KnowledgeClient
from app.backend.knowledge.embeddings import QueryEmbedder
from app.backend.profiles import CompanyProfile, ProfileRegistry
from app.backend.knowledge import ConversationOrchestrator, SimulatedKnowledgeClient
from app.backend.knowledge.routes import router as knowledge_router
from app.backend.settings import REPOSITORY_ROOT, Settings, get_settings

class DemandContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    demand_id: str
    title: str
    description: str = ""
    cpv_codes: list[str] = Field(default_factory=list)
    quantity: str | None = None
    deadline: str | None = None


class BuyerDraftContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "request_quote",
        "request_clarification",
        "decline",
        "escalate",
    ]
    decision: Literal[
        "GO",
        "GO_WITH_CONDITIONS",
        "ESCALATE",
        "NO_GO",
        "CLEARED_TO_REQUEST_QUOTE",
    ]
    evidence_summary: str
    permitted_data: list[str] = Field(default_factory=list)


def _require_profile(profile: CompanyProfile | None, profile_id: str) -> CompanyProfile:
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile {profile_id!r} was not found",
        )
    return profile


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = app_settings
        app.state.profiles = ProfileRegistry.load(app_settings.profiles_dir)
        app.state.buyer_agent_directory = BuyerAgentDirectory(app.state.profiles)
        # Company agents and their conversations, on the simulated knowledge database for now.
        companies = {p.id: p.model_dump() for p in [*app.state.profiles.buyers.values(), *app.state.profiles.suppliers.values()]}
        app.state.knowledge = SimulatedKnowledgeClient(REPOSITORY_ROOT / "data" / "demo" / "agent_db.json", companies)
        # Demo flow: agents negotiate on their own (the buyer agent first asks about the deal check);
        # a person approves only the agreed deal at the end (POST /v1/conversations/{id}/approval).
        app.state.orchestrator = ConversationOrchestrator(app.state.knowledge, companies, app_settings.gemini_model,
                                                          clarify_first=True)
        app.state.database_engine = create_database_engine(app_settings)
        app.state.knowledge_client = (
            KnowledgeClient(app.state.database_engine)
            if app.state.database_engine is not None
            else None
        )
        app.state.query_embedder = QueryEmbedder.from_settings(app_settings)
        if app_settings.google_api_key is not None:
            configure_gemini(app_settings)
        try:
            yield
        finally:
            for task in list(app.state.orchestrator.tasks):
                task.cancel()
            if app.state.database_engine is not None:
                await app.state.database_engine.dispose()

    app = FastAPI(
        title="SalesPatriot Backend",
        version="0.1.0",
        lifespan=lifespan,
    )
    from app.backend.demo import router as demo_router
    from app.backend.agents.peer import BuyerAgentDirectory, router as peer_agent_router
    from app.backend.knowledge.routes import router as knowledge_router

    app.include_router(demo_router)
    app.include_router(knowledge_router)
    app.include_router(peer_agent_router)
    app.include_router(knowledge_router)

    def request_settings(request: Request) -> Settings:
        return request.app.state.settings

    def request_profiles(request: Request) -> ProfileRegistry:
        return request.app.state.profiles

    @app.get("/health/live")
    async def liveness() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health/ready")
    async def readiness(request: Request) -> dict[str, str]:
        engine = request.app.state.database_engine
        if engine is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="DATABASE_URL is unset or still contains a placeholder",
            )
        try:
            await check_database_connection(engine)
        except SQLAlchemyError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Supabase PostgreSQL connection failed",
            ) from error
        return {"status": "ready", "database": "connected"}

    @app.get("/v1/profiles/summary")
    async def profile_summary(
        profiles: Annotated[ProfileRegistry, Depends(request_profiles)],
    ) -> dict[str, int]:
        return {
            "buyers": len(profiles.buyers),
            "suppliers": len(profiles.suppliers),
        }

    @app.post(
        "/v1/agents/suppliers/{profile_id}/preview",
        response_model=SupplierIntroductionDraft,
    )
    async def supplier_preview(
        profile_id: str,
        demand: DemandContext,
        profiles: Annotated[ProfileRegistry, Depends(request_profiles)],
        current_settings: Annotated[Settings, Depends(request_settings)],
    ) -> SupplierIntroductionDraft:
        supplier = _require_profile(profiles.get_supplier(profile_id), profile_id)
        agent_settings = supplier.agent_settings
        if (
            agent_settings is None
            or not agent_settings.enabled
            or not set(demand.cpv_codes).intersection(agent_settings.opted_in_categories)
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Supplier agent is not opted in for this demand category",
            )
        if current_settings.google_api_key is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GOOGLE_API_KEY is not configured",
            )

        configure_gemini(current_settings)
        agent = build_supplier_agent(profile_id, current_settings.gemini_model)
        payload: dict[str, Any] = {
            "supplier_profile": {
                "id": supplier.id,
                "name": supplier.name,
                "country": supplier.country,
                "top_cpv": getattr(supplier, "top_cpv", []),
                "capacity_declarations": agent_settings.capacity_declarations,
            },
            "demand": demand.model_dump(),
        }
        try:
            return await run_structured_agent(
                agent,
                SupplierIntroductionDraft,
                user_id=profile_id,
                payload=payload,
            )
        except AgentRuntimeError as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(error),
            ) from error

    @app.post(
        "/v1/agents/buyers/{profile_id}/draft-message",
        response_model=BuyerMessageDraft,
    )
    async def buyer_message_draft(
        profile_id: str,
        context: BuyerDraftContext,
        profiles: Annotated[ProfileRegistry, Depends(request_profiles)],
        current_settings: Annotated[Settings, Depends(request_settings)],
    ) -> BuyerMessageDraft:
        buyer = _require_profile(profiles.get_buyer(profile_id), profile_id)
        policy = buyer.agent_policy
        if policy is None or policy.status != "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Buyer has no active, buyer-approved agent policy",
            )
        if context.action not in policy.permitted_actions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Requested communication action is not permitted by buyer policy",
            )
        if current_settings.google_api_key is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GOOGLE_API_KEY is not configured",
            )

        configure_gemini(current_settings)
        agent = build_buyer_agent(profile_id, current_settings.gemini_model)
        payload = {
            "buyer_profile": {
                "id": buyer.id,
                "name": buyer.name,
                "country": buyer.country,
            },
            "policy_version": policy.version,
            "authorized_action": context.action,
            "decision": context.decision,
            "evidence_summary": context.evidence_summary,
            "permitted_data": context.permitted_data,
        }
        try:
            draft = await run_structured_agent(
                agent,
                BuyerMessageDraft,
                user_id=profile_id,
                payload=payload,
            )
        except AgentRuntimeError as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(error),
            ) from error
        if draft.action != context.action:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Agent output exceeded the buyer-authorized action",
            )
        return draft

    return app


app = create_app()
