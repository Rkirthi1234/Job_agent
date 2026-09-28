"""LLM provider abstraction.

Everything provider-specific lives in this one file. The rest of the
app only ever calls `LLMService.complete_json(...)` and has no idea
whether the request went to Ollama, OpenAI, Azure OpenAI, or OpenRouter.
"""
import json
import logging

from openai import APIError, APITimeoutError, OpenAI

from app.config import get_settings

logger = logging.getLogger(__name__)


class LLMServiceError(Exception):
    """Raised when the LLM call fails or returns something unusable."""


class LLMService:
    """Thin wrapper around an OpenAI-compatible chat completions endpoint.

    Ollama, OpenAI, Azure OpenAI, Microsoft Foundry, and OpenRouter all
    expose (or can expose) an OpenAI-compatible /chat/completions API,
    so one client class covers all of them — only base_url, api_key,
    and model differ between providers, and all three are read from
    `.env` via app.config.Settings. Swapping providers later never
    requires touching resume_agent.py or resume_service.py.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self.model = settings.llm_model
        self.json_response_format_supported = settings.llm_json_response_format
        # Ollama ignores the API key, but the OpenAI SDK requires a
        # non-empty string to be passed in regardless.
        api_key = settings.openai_api_key or "not-needed-for-ollama"
        self.client = OpenAI(
            base_url=settings.llm_base_url,
            api_key=api_key,
            timeout=settings.llm_timeout_seconds,
        )

    def complete_json(self, system_prompt: str, user_prompt: str) -> dict:
        """Send a prompt and return the response parsed as a JSON dict.

        Raises LLMServiceError on any timeout, API failure, or response
        that isn't valid JSON, so callers never deal with raw provider
        exceptions directly.
        """
        request_kwargs: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
        }
        if self.json_response_format_supported:
            request_kwargs["response_format"] = {"type": "json_object"}

        try:
            response = self.client.chat.completions.create(**request_kwargs)
        except APITimeoutError as exc:
            raise LLMServiceError("LLM request timed out.") from exc
        except APIError as exc:
            raise LLMServiceError(f"LLM provider returned an error: {exc}") from exc
        except Exception as exc:  # connection refused, DNS failure, etc.
            raise LLMServiceError(f"Could not reach LLM provider: {exc}") from exc

        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise LLMServiceError("LLM returned an empty response.")

        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            logger.warning("LLM returned invalid JSON: %s", content[:500])
            raise LLMServiceError("LLM returned invalid JSON.") from exc
