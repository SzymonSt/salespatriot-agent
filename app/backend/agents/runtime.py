import json
import os
import re
from typing import TypeVar

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from google.genai.errors import APIError
from pydantic import BaseModel, ValidationError

from app.backend.settings import Settings

OutputModel = TypeVar("OutputModel", bound=BaseModel)


class AgentRuntimeError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        provider_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_detail = provider_detail


def _safe_provider_detail(message: str | None) -> str | None:
    if not message:
        return None
    sanitized = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[redacted API key]", message)
    sanitized = re.sub(r"(?i)Bearer\s+\S+", "Bearer [redacted]", sanitized)
    sanitized = " ".join(sanitized.split())
    return sanitized[:500] or None


def configure_gemini(settings: Settings) -> None:
    if settings.google_api_key is None:
        raise AgentRuntimeError("GOOGLE_API_KEY is not configured")
    os.environ["GOOGLE_API_KEY"] = settings.google_api_key.get_secret_value()


async def run_structured_agent(
    agent: Agent,
    output_model: type[OutputModel],
    user_id: str,
    payload: dict[str, object],
) -> OutputModel:
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="salespatriot",
        user_id=user_id,
    )
    runner = Runner(
        app_name="salespatriot",
        agent=agent,
        session_service=session_service,
    )
    user_message = types.Content(
        role="user",
        parts=[
            types.Part(
                text=json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )
        ],
    )

    final_text = ""
    try:
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session.id,
            new_message=user_message,
        ):
            if event.error_code:
                raise AgentRuntimeError("ADK agent invocation failed")
            if event.is_final_response() and event.content:
                final_text = "".join(
                    part.text or "" for part in event.content.parts or []
                ).strip()
    except APIError as error:
        provider_status = error.status
        if (
            not isinstance(provider_status, str)
            or not provider_status
            or len(provider_status) > 64
            or not provider_status.isascii()
            or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
                for character in provider_status
            )
        ):
            provider_status = None
        status_detail = f", {provider_status}" if provider_status else ""
        raise AgentRuntimeError(
            f"Gemini API request failed (HTTP {error.code}{status_detail})",
            provider_detail=_safe_provider_detail(error.message),
        ) from error

    if not final_text:
        raise AgentRuntimeError("ADK agent returned no structured response")

    try:
        return output_model.model_validate_json(final_text)
    except ValidationError as error:
        raise AgentRuntimeError("ADK agent returned invalid structured output") from error
