import re
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from app.backend.profiles import CompanyProfile, ProfileRegistry

router = APIRouter()


class PublicReferenceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["buyer_profile_top_counterparts", "buyer_profile_recent_notice"]
    counterpart_notice_count: int | None = None
    notice_number: str | None = None
    title: str | None = None
    published: str | None = None
    ted_url: str | None = None
    cpv_codes: list[str] = Field(default_factory=list)


class BuyerReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    sender_agent_id: str
    recipient_agent_id: str
    supplier_id: str
    supplier_name: str
    scope: Literal["public_ted_profile_evidence"]
    question: str


class BuyerReferenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    sender_agent_id: str
    recipient_agent_id: str
    supplier_id: str
    supplier_name: str
    status: Literal["public_relationship_found"]
    evidence: list[PublicReferenceEvidence]
    performance_assessment: Literal["not_available_in_profile"]
    summary: str


class BuyerAgentOption(BaseModel):
    buyer_id: str
    buyer_name: str
    agent_id: str


class SupplierPeerOption(BaseModel):
    supplier_id: str
    supplier_name: str
    peer_buyer_ids: list[str]


class BuyerAgentOptions(BaseModel):
    buyers: list[BuyerAgentOption]
    suppliers: list[SupplierPeerOption]


class SupplierReferenceCheckInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supplier_id: str
    peer_buyer_id: str


class BuyerReferenceCheckResult(BaseModel):
    requester_buyer: BuyerAgentOption
    peer_buyer: BuyerAgentOption
    supplier_id: str
    supplier_name: str
    request: BuyerReferenceRequest
    response: BuyerReferenceResponse
    data_scope: Literal["public_ted_profile_allowlist"]
    private_profile_data_shared: bool = False
    external_organization_contacted: bool = False
    persisted: bool = False


def _normalize_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(re.sub(r"[^\w]+", " ", value.casefold()).split())


def _extra(profile: CompanyProfile) -> dict[str, object]:
    return profile.model_extra or {}


def _supplier_names(supplier: CompanyProfile) -> set[str]:
    extra = _extra(supplier)
    return {
        normalized
        for normalized in (
            _normalize_name(supplier.name),
            _normalize_name(extra.get("legal_name")),
        )
        if normalized
    }


def _option(profile: CompanyProfile) -> BuyerAgentOption:
    return BuyerAgentOption(
        buyer_id=profile.id,
        buyer_name=profile.name,
        agent_id=f"buyer-agent-{profile.id}",
    )


class BuyerProfileAgent:
    def __init__(self, profile: CompanyProfile) -> None:
        self.profile = profile
        self.agent_id = f"buyer-agent-{profile.id}"

    def public_evidence(
        self,
        supplier: CompanyProfile,
    ) -> list[PublicReferenceEvidence]:
        supplier_names = _supplier_names(supplier)
        if not supplier_names:
            return []

        evidence: list[PublicReferenceEvidence] = []
        seen_notices: set[str] = set()
        buyer_data = _extra(self.profile)

        counterparts = buyer_data.get("top_counterparts", [])
        if isinstance(counterparts, list):
            for counterpart in counterparts:
                if not isinstance(counterpart, dict):
                    continue
                if _normalize_name(counterpart.get("name")) not in supplier_names:
                    continue
                notice_count = counterpart.get("notices")
                evidence.append(
                    PublicReferenceEvidence(
                        source="buyer_profile_top_counterparts",
                        counterpart_notice_count=(
                            notice_count
                            if isinstance(notice_count, int)
                            and not isinstance(notice_count, bool)
                            else None
                        ),
                    )
                )

        recent_notices = buyer_data.get("recent_notices", [])
        if isinstance(recent_notices, list):
            for notice in recent_notices:
                if not isinstance(notice, dict):
                    continue
                names = notice.get("counterparts", [])
                if not isinstance(names, list) or not any(
                    _normalize_name(name) in supplier_names for name in names
                ):
                    continue
                notice_number = notice.get("notice_number")
                if not isinstance(notice_number, str) or notice_number in seen_notices:
                    continue
                seen_notices.add(notice_number)
                cpv_codes = notice.get("cpv_codes", [])
                evidence.append(
                    PublicReferenceEvidence(
                        source="buyer_profile_recent_notice",
                        notice_number=notice_number,
                        title=(
                            notice.get("title")
                            if isinstance(notice.get("title"), str)
                            else notice.get("headline")
                            if isinstance(notice.get("headline"), str)
                            else None
                        ),
                        published=(
                            notice.get("published")
                            if isinstance(notice.get("published"), str)
                            else None
                        ),
                        ted_url=(
                            notice.get("ted_url")
                            if isinstance(notice.get("ted_url"), str)
                            else None
                        ),
                        cpv_codes=(
                            [code for code in cpv_codes if isinstance(code, str)]
                            if isinstance(cpv_codes, list)
                            else []
                        ),
                    )
                )

        return evidence

    def create_reference_request(
        self,
        peer: "BuyerProfileAgent",
        supplier: CompanyProfile,
    ) -> BuyerReferenceRequest:
        return BuyerReferenceRequest(
            message_id=str(uuid4()),
            sender_agent_id=self.agent_id,
            recipient_agent_id=peer.agent_id,
            supplier_id=supplier.id,
            supplier_name=supplier.name,
            scope="public_ted_profile_evidence",
            question=(
                "Share only public TED profile evidence of a procurement "
                "relationship with this supplier. Do not disclose private "
                "procurement data, KYC, due diligence, internal communications, "
                "or subjective delivery or quality assessments."
            ),
        )

    def respond_to_reference_request(
        self,
        request: BuyerReferenceRequest,
        supplier: CompanyProfile,
    ) -> BuyerReferenceResponse:
        if request.recipient_agent_id != self.agent_id:
            raise ValueError("Reference request was routed to the wrong buyer agent")
        evidence = self.public_evidence(supplier)
        if not evidence:
            raise ValueError("Buyer profile contains no matching public evidence")

        notice_counts = [
            item.counterpart_notice_count
            for item in evidence
            if item.counterpart_notice_count is not None
        ]
        if notice_counts:
            relationship = (
                f"The public buyer profile lists this supplier as a counterpart "
                f"for {max(notice_counts)} notice(s)."
            )
        else:
            relationship = (
                "The public buyer profile contains notice records naming this supplier."
            )
        return BuyerReferenceResponse(
            message_id=str(uuid4()),
            sender_agent_id=self.agent_id,
            recipient_agent_id=request.sender_agent_id,
            supplier_id=supplier.id,
            supplier_name=supplier.name,
            status="public_relationship_found",
            evidence=evidence,
            performance_assessment="not_available_in_profile",
            summary=(
                f"{relationship} The profile does not contain delivery or quality "
                "assessments; this is not an endorsement or a performance reference."
            ),
        )


class BuyerAgentDirectory:
    def __init__(self, profiles: ProfileRegistry) -> None:
        self.buyers = {
            profile_id: BuyerProfileAgent(profile)
            for profile_id, profile in profiles.buyers.items()
        }
        self.suppliers = profiles.suppliers

    def options(self) -> BuyerAgentOptions:
        buyers = sorted(
            (agent.profile for agent in self.buyers.values()),
            key=lambda profile: profile.name.casefold(),
        )
        suppliers: list[SupplierPeerOption] = []
        for supplier in self.suppliers.values():
            peer_buyer_ids = sorted(
                agent.profile.id
                for agent in self.buyers.values()
                if agent.public_evidence(supplier)
            )
            if peer_buyer_ids:
                suppliers.append(
                    SupplierPeerOption(
                        supplier_id=supplier.id,
                        supplier_name=supplier.name,
                        peer_buyer_ids=peer_buyer_ids,
                    )
                )
        suppliers.sort(key=lambda supplier: supplier.supplier_name.casefold())
        return BuyerAgentOptions(
            buyers=[_option(profile) for profile in buyers],
            suppliers=suppliers,
        )

    def run_reference_check(
        self,
        requester_buyer_id: str,
        supplier_id: str,
        peer_buyer_id: str,
    ) -> BuyerReferenceCheckResult:
        requester = self.buyers.get(requester_buyer_id)
        if requester is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Buyer profile {requester_buyer_id!r} was not found",
            )
        peer = self.buyers.get(peer_buyer_id)
        if peer is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Peer buyer profile {peer_buyer_id!r} was not found",
            )
        if requester_buyer_id == peer_buyer_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A buyer agent cannot request a reference from itself",
            )
        supplier = self.suppliers.get(supplier_id)
        if supplier is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Supplier profile {supplier_id!r} was not found",
            )
        if not peer.public_evidence(supplier):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Peer buyer profile has no matching public TED evidence for "
                    "this supplier"
                ),
            )

        request = requester.create_reference_request(peer, supplier)
        response = peer.respond_to_reference_request(request, supplier)
        return BuyerReferenceCheckResult(
            requester_buyer=_option(requester.profile),
            peer_buyer=_option(peer.profile),
            supplier_id=supplier.id,
            supplier_name=supplier.name,
            request=request,
            response=response,
            data_scope="public_ted_profile_allowlist",
        )


@router.get("/v1/agents/reference-options", response_model=BuyerAgentOptions)
async def reference_options(request: Request) -> BuyerAgentOptions:
    return request.app.state.buyer_agent_directory.options()


@router.post(
    "/v1/agents/buyers/{requester_buyer_id}/reference-requests",
    response_model=BuyerReferenceCheckResult,
)
async def create_reference_request(
    requester_buyer_id: str,
    body: SupplierReferenceCheckInput,
    request: Request,
) -> BuyerReferenceCheckResult:
    return request.app.state.buyer_agent_directory.run_reference_check(
        requester_buyer_id,
        body.supplier_id,
        body.peer_buyer_id,
    )
