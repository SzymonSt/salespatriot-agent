"""Request models for the knowledge, memory and conversation routes (contract §3.6)."""

from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = Literal["buyer", "supplier"]
Purpose = Literal[
    "assess_tender",
    "find_suppliers",
    "evaluate_quote",
    "check_counterparty",
    "search",
]
ChunkKind = Literal[
    "tender",
    "award",
    "contract",
    "supplier_profile",
    "buyer_profile",
    "catalogue_item",
    "conversation",
    "kb_doc",
    "pattern",
]
SenderRole = Literal["buyer", "supplier", "system"]
MessageType = Literal[
    "introduction",
    "rfq",
    "quote",
    "counter",
    "accept",
    "reject",
    "question",
    "answer",
    "reference_request",
    "reference_answer",
    "escalate",
    "check",
]

SUBJECT_KEYS = frozenset(
    {
        "publication_number",
        "supplier_id",
        "buyer_id",
        "family_id",
        "calibre",
        "country",
        "conversation_id",
        "message_id",
    }
)

# Message types each side may send (the database state machine checks order).
SENDER_MESSAGE_TYPES: dict[str, frozenset[str]] = {
    "buyer": frozenset(
        {"rfq", "counter", "accept", "reject", "question", "reference_request", "escalate", "check"}
    ),
    "supplier": frozenset(
        {"introduction", "quote", "answer", "reference_answer", "accept", "reject", "escalate"}
    ),
    "system": frozenset({"check", "escalate"}),
}

COUNTRY_PATTERN = r"^[A-Z]{3}$"
CURRENCY_PATTERN = r"^[A-Z]{3}$"
IdField = Field(min_length=1, max_length=200)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def validate_subject(purpose: str, subject: dict[str, Any], query: str | None) -> None:
    unknown = set(subject) - SUBJECT_KEYS
    if unknown:
        raise ValueError(f"subject has unknown keys: {sorted(unknown)}")
    for key, value in subject.items():
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 200):
            raise ValueError(f"subject.{key} must be a non-empty string")

    def has(key: str) -> bool:
        return bool(subject.get(key))

    if purpose == "assess_tender" and not (has("publication_number") and has("supplier_id")):
        raise ValueError("assess_tender needs subject.publication_number and subject.supplier_id")
    if purpose == "find_suppliers" and not (has("publication_number") or has("family_id")):
        raise ValueError("find_suppliers needs subject.publication_number or subject.family_id")
    if purpose == "evaluate_quote" and not (has("conversation_id") and has("message_id")):
        raise ValueError("evaluate_quote needs subject.conversation_id and subject.message_id")
    if purpose == "check_counterparty" and not (has("supplier_id") or has("buyer_id")):
        raise ValueError("check_counterparty needs subject.supplier_id or subject.buyer_id")
    if purpose == "search" and not (query or "").strip():
        raise ValueError("search needs a query")


class ContextRequest(StrictModel):
    role: Role
    purpose: Purpose
    subject: dict[str, Any] = Field(default_factory=dict)
    query: str | None = Field(default=None, max_length=2_000)
    viewer_id: str = IdField
    limit: int = Field(default=8, ge=1, le=20)

    @model_validator(mode="after")
    def check_subject(self) -> Self:
        validate_subject(self.purpose, self.subject, self.query)
        return self


class KnowledgeSearchRequest(StrictModel):
    query: str = Field(min_length=1, max_length=2_000)
    kinds: list[ChunkKind] | None = None
    family_id: str | None = Field(default=None, max_length=200)
    calibre: str | None = Field(default=None, max_length=50)
    cpv_prefix: str | None = Field(default=None, pattern=r"^\d{2,8}$")
    country: str | None = Field(default=None, pattern=COUNTRY_PATTERN)
    viewer_id: str = IdField
    limit: int = Field(default=8, ge=1, le=20)


class MemoryRecallRequest(StrictModel):
    query: str = Field(min_length=1, max_length=2_000)
    role: Role
    family_id: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, pattern=COUNTRY_PATTERN)
    counterparty: str | None = Field(default=None, max_length=200)
    viewer_id: str = IdField
    limit: int = Field(default=5, ge=1, le=20)


class MemoryWriteRequest(StrictModel):
    """Agents may write episodes, preferences and pattern proposals, always as proposed.

    Lessons and validated entries come only from service-role jobs (WS6), and
    platform-wide (``public``) visibility is reserved for validated lessons.
    """

    kind: Literal["episode", "preference", "pattern_proposal"]
    status: Literal["proposed"] = "proposed"
    role: Role
    owner_id: str = IdField
    text: str = Field(min_length=1, max_length=4_000)
    family_id: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, pattern=COUNTRY_PATTERN)
    counterparty: str | None = Field(default=None, max_length=200)
    facts: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)
    visibility: Literal["owner", "parties"] = "owner"
    party_ids: list[str] = Field(default_factory=list, max_length=20)
    conversation_id: UUID | None = None

    @model_validator(mode="after")
    def episode_needs_conversation(self) -> Self:
        if self.kind == "episode" and self.conversation_id is None:
            raise ValueError("an episode needs conversation_id")
        return self

    def to_entry(self) -> dict[str, Any]:
        entry = self.model_dump(mode="json", exclude_none=True)
        entry["writer"] = "agent"  # memory_write: agents may only propose
        if self.visibility == "parties" and self.owner_id not in self.party_ids:
            entry["party_ids"] = [*self.party_ids, self.owner_id]
        return entry


class MemoryFeedbackRequest(StrictModel):
    helpful: bool


class Mandate(StrictModel):
    """What one side's agent may do on its own (K11). Checked in Python, never by the model.

    The first four fields mirror ``agent_mandates`` / ``deal_due_diligence.DEFAULT_MANDATE``.
    ``price_ceiling`` (buyer, total) and ``min_unit_price`` (supplier) are in ``currency``.
    """

    source: str | None = Field(default=None, max_length=200)
    max_autonomous_value_eur: float = Field(default=250_000, ge=0)
    min_supplier_score: float = Field(default=6.0, ge=0, le=10)
    allowed_kyc_status: list[str] = Field(default_factory=lambda: ["verified", "limited"])
    allowed_cpv_prefixes: list[str] = Field(
        default_factory=lambda: ["3531", "3532", "3533", "3534"]
    )
    max_rounds: int = Field(default=2, ge=0, le=5)
    price_ceiling: float | None = Field(default=None, gt=0)
    min_unit_price: float | None = Field(default=None, gt=0)
    currency: str | None = Field(default=None, pattern=CURRENCY_PATTERN)
    min_evidence_refs: int = Field(default=1, ge=0, le=10)

    @model_validator(mode="after")
    def price_limits_need_currency(self) -> Self:
        if (self.price_ceiling is not None or self.min_unit_price is not None) and not self.currency:
            raise ValueError("price_ceiling and min_unit_price need a currency")
        return self


class CreateConversationRequest(StrictModel):
    publication_number: str = IdField
    buyer_id: str = IdField
    supplier_id: str = IdField
    buyer_mandate: Mandate | None = None
    supplier_mandate: Mandate | None = None
    demo: bool = True
    run: Literal["introduction", "full"] = "introduction"


class PostMessageRequest(StrictModel):
    sender_role: SenderRole
    sender_id: str = IdField
    type: MessageType
    text: str | None = Field(default=None, max_length=4_000)
    payload: dict[str, Any] = Field(default_factory=dict)
    refs: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def type_matches_sender(self) -> Self:
        if self.type not in SENDER_MESSAGE_TYPES[self.sender_role]:
            raise ValueError(f"a {self.sender_role} cannot send a {self.type!r} message")
        return self


class OutcomeRequest(StrictModel):
    """``deal_outcomes`` fields, recorded through ``agent_conversation_close``."""

    delivered_on_time: bool | None = None
    quality: str | None = Field(default=None, max_length=200)
    final_price: float | None = Field(default=None, ge=0)
    unit_price: float | None = Field(default=None, ge=0)
    quantity: float | None = Field(default=None, gt=0)
    unit: str | None = Field(default=None, max_length=50)
    currency: str | None = Field(default=None, pattern=CURRENCY_PATTERN)
    notes: str | None = Field(default=None, max_length=4_000)
    reported_by: str = IdField


class RunLiveDemoRequest(StrictModel):
    publication_number: str | None = Field(default=None, max_length=200)
    supplier_id: str | None = Field(default=None, max_length=200)
    buyer_mandate: Mandate | None = None
    supplier_mandate: Mandate | None = None
