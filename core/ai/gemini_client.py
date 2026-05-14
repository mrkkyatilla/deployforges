from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from google import genai
from google.genai import types

from api.config import settings
from core.ai.json_response import mask_secrets, truncate_for_log

logger = logging.getLogger(__name__)


@dataclass
class AIResponse:
    text: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str
    latency_ms: int
    excerpt_prompt: str | None = None
    excerpt_response: str | None = None


class GeminiClient:
    """Gemini API wrapper with retry, timeout, and token tracking."""

    def __init__(self):
        self._client = genai.Client(api_key=settings.gemini_api_key)

    def _maybe_excerpts(self, prompt: str, response_text: str) -> tuple[str | None, str | None]:
        if not (settings.ai_debug_io or settings.ai_persist_io_excerpts):
            return None, None
        max_c = max(256, settings.ai_debug_max_chars)
        masked_p = mask_secrets(prompt, settings.secret_patterns)
        masked_r = mask_secrets(response_text or "", settings.secret_patterns)
        return (
            truncate_for_log(masked_p, max_c),
            truncate_for_log(masked_r, max_c),
        )

    def _maybe_log_io(
        self,
        *,
        label: str,
        model: str,
        prompt: str,
        response_text: str,
    ) -> None:
        if not settings.ai_debug_io:
            return
        pe, re = self._maybe_excerpts(prompt, response_text)
        msg = (
            "Gemini IO label=%s model=%s prompt_chars=%d response_chars=%d\n"
            "--- prompt excerpt ---\n%s\n--- response excerpt ---\n%s"
        )
        logger.info(
            msg,
            label,
            model,
            len(prompt),
            len(response_text or ""),
            pe or "",
            re or "",
        )

    async def generate(
        self,
        *,
        prompt: str,
        system_instruction: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        max_output_tokens: int = 4096,
        response_mime_type: str | None = None,
        response_schema: dict[str, Any] | types.Schema | None = None,
        io_log_label: str = "",
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
        if response_schema is not None:
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

                    await asyncio.sleep(2**attempt)
        else:
            msg = f"Gemini API failed after {settings.gemini_max_retries} retries"
            raise RuntimeError(msg) from last_error

        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        usage = response.usage_metadata
        text = response.text or ""

        ep, er = self._maybe_excerpts(prompt, text)
        self._maybe_log_io(
            label=io_log_label or "gemini",
            model=model,
            prompt=prompt,
            response_text=text,
        )

        return AIResponse(
            text=text,
            prompt_tokens=usage.prompt_token_count if usage else 0,
            completion_tokens=usage.candidates_token_count if usage else 0,
            total_tokens=usage.total_token_count if usage else 0,
            model=model,
            latency_ms=elapsed_ms,
            excerpt_prompt=ep,
            excerpt_response=er,
        )

    async def generate_json(
        self,
        *,
        prompt: str,
        system_instruction: str | None = None,
        model: str | None = None,
        temperature: float = 0.1,
        max_output_tokens: int = 4096,
        response_schema: dict[str, Any] | types.Schema | None = None,
        io_log_label: str = "",
    ) -> AIResponse:
        return await self.generate(
            prompt=prompt,
            system_instruction=system_instruction,
            model=model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_mime_type="application/json",
            response_schema=response_schema,
            io_log_label=io_log_label,
        )
