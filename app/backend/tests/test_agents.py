import pytest
from pydantic import ValidationError

from app.backend.agents.main import (
    BuyerMessageDraft,
    SupplierIntroductionDraft,
    build_buyer_agent,
    build_supplier_agent,
)


def test_gemini_output_schemas_omit_unsupported_additional_properties() -> None:
    for output_model in (SupplierIntroductionDraft, BuyerMessageDraft):
        assert "additionalProperties" not in output_model.model_json_schema()

    with pytest.raises(ValidationError):
        SupplierIntroductionDraft.model_validate(
            {
                "non_binding_message": "Non-binding demo message",
                "unexpected": "field",
            }
        )


def test_agent_factories_use_configured_gemini_model_and_output_schema() -> None:
    supplier_agent = build_supplier_agent("supplier-pl-123", "gemini-2.5-flash")
    buyer_agent = build_buyer_agent("buyer-pl-456", "gemini-2.5-flash")

    assert supplier_agent.model == "gemini-2.5-flash"
    assert supplier_agent.output_schema is SupplierIntroductionDraft
    assert buyer_agent.model == "gemini-2.5-flash"
    assert buyer_agent.output_schema is BuyerMessageDraft


def test_adk_agent_names_are_normalized() -> None:
    agent = build_supplier_agent("supplier-pl-123", "gemini-2.5-flash")

    assert agent.name == "supplier_supplier_pl_123"
