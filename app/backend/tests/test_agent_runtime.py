import pytest
from google.adk.agents import Agent
from google.genai.errors import APIError
from pydantic import BaseModel

from app.backend.agents.runtime import AgentRuntimeError, run_structured_agent


class OutputModel(BaseModel):
    result: str


@pytest.mark.asyncio
async def test_gemini_api_error_exposes_only_safe_provider_status(monkeypatch) -> None:
    class FailedRunner:
        def __init__(self, **kwargs) -> None:
            pass

        def run_async(self, **kwargs):
            async def events():
                raise APIError(
                    403,
                    {
                        "error": {
                            "status": "PERMISSION_DENIED",
                                "message": (
                                    "Sensitive provider detail "
                                    "AIzaSy0123456789abcdefghijklmNOPQRSTUV"
                                ),
                            }
                        },
                    )
                yield None

            return events()

    monkeypatch.setattr("app.backend.agents.runtime.Runner", FailedRunner)

    with pytest.raises(
        AgentRuntimeError,
        match=r"Gemini API request failed \(HTTP 403, PERMISSION_DENIED\)",
    ) as error:
        await run_structured_agent(
            agent=Agent(
                name="test_agent",
                model="gemini-2.5-flash",
                instruction="test",
            ),
            output_model=OutputModel,
            user_id="test-user",
            payload={},
        )

    assert error.value.provider_detail == (
        "Sensitive provider detail [redacted API key]"
    )
