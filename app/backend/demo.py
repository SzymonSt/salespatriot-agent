from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

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

router = APIRouter()
DEMO_PAGE = Path(__file__).resolve().parent / "static" / "demo.html"

DEMO_DEMAND = {
    "id": "demo-demand-001",
    "title": "Standard industrial maintenance toolkits",
    "category": "DEMO-TOOLS",
    "quantity": 100,
    "delivery_window_days": 30,
    "controlled_item": False,
}

DEMO_BUYER = {
    "id": "demo-buyer-001",
    "name": "Demo Public Buyer",
    "policy_version": "demo-policy-v1",
    "permitted_actions": ["request_quote"],
    "configured_checks": [
        {"id": "D1", "gate": "hard"},
        {"id": "D2", "gate": "hard"},
        {"id": "D3", "gate": "hard"},
        {"id": "D4", "gate": "hard"},
        {"id": "D5", "gate": "hard"},
        {"id": "D6", "gate": "hard"},
        {"id": "D13", "gate": "score"},
        {"id": "D14", "gate": "hard"},
    ],
}

DEMO_SUPPLIERS = [
    {
        "id": "demo-supplier-north",
        "name": "North Demo Tools",
        "enabled": True,
        "opted_in_categories": ["DEMO-TOOLS"],
        "declared_capacity": 130,
        "lead_time_days": 14,
        "unit_price_eur": 52.50,
        "quality_score": 0.96,
    },
    {
        "id": "demo-supplier-east",
        "name": "East Demo Industrial",
        "enabled": True,
        "opted_in_categories": ["DEMO-TOOLS"],
        "declared_capacity": 110,
        "lead_time_days": 27,
        "unit_price_eur": 49.75,
        "quality_score": 0.93,
    },
]

DEMO_REFERENCE = {
    "agent_id": "demo-peer-buyer-agent",
    "relationship": "Seeded synthetic prior transaction",
    "response": {
        "delivery": "on_time",
        "quantity": "complete",
        "quality_issues": "none_reported",
        "source_status": "simulated_demo_fixture",
    },
}


class DemoStep(BaseModel):
    stage: str
    actor: str
    status: str
    details: dict[str, Any]


class DemoQuote(BaseModel):
    supplier_id: str
    supplier_name: str
    quantity: int
    unit_price_eur: float
    total_price_eur: float
    lead_time_days: int
    quality_score: float
    illustrative: bool = True


class DemoRunResult(BaseModel):
    run_id: str
    demo_only: bool = True
    persisted: bool = False
    external_messages_sent: bool = False
    model: str
    demand: dict[str, Any]
    timeline: list[DemoStep]
    buyer_quote_request: str
    quotes: list[DemoQuote]
    selected_quote: DemoQuote
    disclaimer: str


def _due_diligence_checks(supplier: dict[str, Any]) -> list[dict[str, str]]:
    capacity_passes = (
        supplier["declared_capacity"] >= DEMO_DEMAND["quantity"]
        and supplier["lead_time_days"] <= DEMO_DEMAND["delivery_window_days"]
    )
    return [
        {
            "id": "D1",
            "status": "pass",
            "evidence": "Synthetic demo KYC fixture; not a real check",
        },
        {
            "id": "D2",
            "status": "pass",
            "evidence": "Synthetic buyer policy permits the non-controlled demo item",
        },
        {
            "id": "D3",
            "status": "pass",
            "evidence": "No special licence requirement in this synthetic demo",
        },
        {
            "id": "D4",
            "status": "pass",
            "evidence": "Demo contains no classified or customer data",
        },
        {
            "id": "D5",
            "status": "pass",
            "evidence": "Synthetic technical-fit fixture",
        },
        {
            "id": "D6",
            "status": "pass" if capacity_passes else "fail",
            "evidence": (
                f"Declared capacity {supplier['declared_capacity']} units; "
                f"lead time {supplier['lead_time_days']} days"
            ),
        },
        {
            "id": "D13",
            "status": "pass",
            "evidence": "Simulated peer-buyer reference fixture; not a real reference",
        },
        {
            "id": "D14",
            "status": "pass",
            "evidence": "Synthetic demo mandate permits quote collection only",
        },
    ]


@router.get("/demo", response_class=HTMLResponse, include_in_schema=False)
async def demo_page() -> HTMLResponse:
    return HTMLResponse(DEMO_PAGE.read_text(encoding="utf-8"))


@router.post("/api/demo/run", response_model=DemoRunResult)
async def run_manual_demo(request: Request) -> DemoRunResult:
    settings = request.app.state.settings
    if settings.google_api_key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GOOGLE_API_KEY is not configured in app/backend/.env",
        )

    configure_gemini(settings)
    run_id = str(uuid4())
    timeline = [
        DemoStep(
            stage="demand_ingested",
            actor="demo_event",
            status="completed",
            details={
                "demand_id": DEMO_DEMAND["id"],
                "title": DEMO_DEMAND["title"],
                "source": "synthetic fixture; not a TED notice",
            },
        ),
        DemoStep(
            stage="supplier_matching",
            actor="platform",
            status="completed",
            details={
                "category": DEMO_DEMAND["category"],
                "matched_suppliers": [
                    supplier["id"]
                    for supplier in DEMO_SUPPLIERS
                    if supplier["enabled"]
                    and DEMO_DEMAND["category"] in supplier["opted_in_categories"]
                ],
            },
        ),
    ]

    quote_candidates: list[DemoQuote] = []
    try:
        for supplier in DEMO_SUPPLIERS:
            if (
                not supplier["enabled"]
                or DEMO_DEMAND["category"] not in supplier["opted_in_categories"]
            ):
                continue

            checks = _due_diligence_checks(supplier)
            hard_failures = [
                check["id"]
                for check in checks
                if check["id"] in {"D1", "D2", "D3", "D4", "D5", "D6", "D14"}
                and check["status"] != "pass"
            ]
            supplier_agent = build_supplier_agent(
                supplier["id"],
                settings.gemini_model,
            )
            introduction = await run_structured_agent(
                supplier_agent,
                SupplierIntroductionDraft,
                user_id=supplier["id"],
                payload={
                    "demo_only": True,
                    "supplier": {
                        "name": supplier["name"],
                        "declared_capacity_units": supplier["declared_capacity"],
                        "lead_time_days": supplier["lead_time_days"],
                    },
                    "demand": DEMO_DEMAND,
                },
            )
            timeline.append(
                DemoStep(
                    stage="supplier_agent_introduction",
                    actor=supplier["id"],
                    status="completed",
                    details={
                        "recipient": "demo-buyer-agent",
                        "non_binding_message": introduction.non_binding_message,
                        "caveats": introduction.caveats,
                    },
                )
            )
            timeline.append(
                DemoStep(
                    stage="supplier_reference_request",
                    actor="demo-buyer-agent",
                    status="completed",
                    details={
                        "recipient": DEMO_REFERENCE["agent_id"],
                        "supplier_id": supplier["id"],
                        "relationship": DEMO_REFERENCE["relationship"],
                        "response": DEMO_REFERENCE["response"],
                    },
                )
            )
            timeline.append(
                DemoStep(
                    stage="buyer_due_diligence",
                    actor="demo-buyer-agent",
                    status="blocked" if hard_failures else "passed",
                    details={
                        "policy_version": DEMO_BUYER["policy_version"],
                        "checks": checks,
                        "decision": "NO_GO" if hard_failures else "CLEARED_TO_REQUEST_QUOTE",
                        "hard_failures": hard_failures,
                    },
                )
            )
            if hard_failures:
                continue
            quote_candidates.append(
                DemoQuote(
                    supplier_id=supplier["id"],
                    supplier_name=supplier["name"],
                    quantity=DEMO_DEMAND["quantity"],
                    unit_price_eur=supplier["unit_price_eur"],
                    total_price_eur=round(
                        DEMO_DEMAND["quantity"] * supplier["unit_price_eur"], 2
                    ),
                    lead_time_days=supplier["lead_time_days"],
                    quality_score=supplier["quality_score"],
                )
            )

        if not quote_candidates:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Demo policy found no supplier eligible to receive a quote request",
            )

        buyer_agent = build_buyer_agent(DEMO_BUYER["id"], settings.gemini_model)
        buyer_draft = await run_structured_agent(
            buyer_agent,
            BuyerMessageDraft,
            user_id=DEMO_BUYER["id"],
            payload={
                "demo_only": True,
                "buyer": DEMO_BUYER,
                "authorized_action": "request_quote",
                "decision": "CLEARED_TO_REQUEST_QUOTE",
                "eligible_suppliers": [quote.supplier_id for quote in quote_candidates],
                "evidence_summary": (
                    "Synthetic buyer checks passed. The D13 reference is a "
                    "simulated fixture, not a real reference."
                ),
                "permitted_data": ["demand title", "quantity", "delivery window"],
            },
        )
        if buyer_draft.action != "request_quote":
            raise AgentRuntimeError(
                "Buyer agent did not follow the authorized quote-request action"
            )

        timeline.append(
            DemoStep(
                stage="quote_request",
                actor="demo-buyer-agent",
                status="completed",
                details={
                    "recipient_suppliers": [quote.supplier_id for quote in quote_candidates],
                    "message": buyer_draft.message,
                    "binding": False,
                },
            )
        )
        timeline.append(
            DemoStep(
                stage="quotes_received",
                actor="demo-supplier-agents",
                status="completed",
                details={
                    "source": "synthetic quote fixtures; not real offers",
                    "quotes": [quote.model_dump() for quote in quote_candidates],
                },
            )
        )
        selected_quote = min(
            quote_candidates,
            key=lambda quote: (quote.total_price_eur, quote.lead_time_days),
        )
        timeline.append(
            DemoStep(
                stage="quote_comparison",
                actor="demo-buyer-agent",
                status="completed",
                details={
                    "criteria": ["within delivery window", "lowest total price", "lead-time tie-break"],
                    "selected_supplier": selected_quote.supplier_id,
                    "selected_total_eur": selected_quote.total_price_eur,
                    "final_award": False,
                },
            )
        )
    except AgentRuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"{error}: {error.provider_detail}"
                if error.provider_detail
                else str(error)
            ),
        ) from error

    return DemoRunResult(
        run_id=run_id,
        model=settings.gemini_model,
        demand=DEMO_DEMAND,
        timeline=timeline,
        buyer_quote_request=buyer_draft.message,
        quotes=quote_candidates,
        selected_quote=selected_quote,
        disclaimer=(
            "This is a local simulation using synthetic companies, checks, "
            "references, and quotes. It sends no external messages, submits no "
            "bid, makes no binding offer, and does not persist the run."
        ),
    )
