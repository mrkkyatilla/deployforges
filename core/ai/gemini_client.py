from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from google import genai
from google.genai import types

from api.config import settings

logger = logging.getLogger(__name__)


@dataclass
class AIResponse:
    text: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str
    latency_ms: int


class GeminiClient:
    """Gemini API wrapper with retry, timeout, and token tracking."""

    def __init__(self):
        self._client = genai.Client(api_key=settings.gemini_api_key)

    async def generate(
        self,
        *,
        prompt: str,
        system_instruction: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        max_output_tokens: int = 4096,
        response_mime_type: str | None = None,
        response_schema: dict | None = None,
    ) -> AIResponse:
        model = model or settings.gemini_pro_model
        start = time.perf_counter_ns()

        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            system_instruction=system_instruction,
        )
        if response_mime_type:
            config.response_mime_type = response_mime_type
        if response_schema:
            config.response_schema = response_schema

        last_error: Exception | None = None
        for attempt in range(1, settings.gemini_max_retries + 1):
            try:
                response = await self._client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config,
                )
                break
            except Exception as exc:
                last_error = exc
                logger.warning("Gemini API attempt %d failed: %s", attempt, exc)
                if attempt < settings.gemini_max_retries:
                    import asyncio
                    await asyncio.sleep(2 ** attempt)
        else:
            raise RuntimeError(f"Gemini API failed after {settings.gemini_max_retries} retries") from last_error

        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        usage = response.usage_metadata

        return AIResponse(
            text=response.text or "",
            prompt_tokens=usage.prompt_token_count if usage else 0,
            completion_tokens=usage.candidates_token_count if usage else 0,
            total_tokens=usage.total_token_count if usage else 0,
            model=model,
            latency_ms=elapsed_ms,
        )

    async def generate_json(
        self,
        *,
        prompt: str,
        system_instruction: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        max_output_tokens: int = 4096,
    ) -> AIResponse:
        return await self.generate(
            prompt=prompt,
            system_instruction=system_instruction,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
        )
