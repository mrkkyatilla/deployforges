from __future__ import annotations

import asyncio
import json
import logging
import random
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
    # When Gemini SDK fills ``response.parsed`` for schema/JSON mode, mirrored here for parsers.
    parsed_dict: dict[str, Any] | None = None


def is_transient_gemini_http_error(exc: BaseException | None) -> bool:
    """503 overload, 429 quota, common deadline signals — worth backing off or switching model."""
    if exc is None:
        return False
    s = str(exc)
    low = s.lower()
    if "503" in s and ("unavailable" in low or "high demand" in low):
        return True
    if "429" in s or "resource_exhausted" in low:
        return True
    if "504" in s and "unavailable" in low:
        return True
    if "deadline exceeded" in low:
        return True
    return False


def _retry_delay_seconds(attempt: int, exc: Exception | None) -> float:
    """Sleep before the next HTTP attempt (``attempt`` is 1-based index of the failed try)."""
    base = float(settings.gemini_retry_backoff_base_seconds)
    cap = float(settings.gemini_retry_backoff_max_seconds)
    raw = min(cap, base * (2 ** (attempt - 1)))
    if is_transient_gemini_http_error(exc):
        raw = min(cap, raw * 1.5)
    jitter = float(settings.gemini_retry_backoff_jitter_ratio)
    if jitter > 0:
        raw *= max(0.1, 1.0 - jitter + (2 * jitter * random.random()))
    return max(1.0, raw)


def _parsed_to_json_text(parsed: Any) -> str | None:
    """Serialize SDK ``parsed`` field (schema mode) for downstream JSON parsers."""
    if parsed is None:
        return None
    if isinstance(parsed, str):
        s = parsed.strip()
        return s if s else None
    if isinstance(parsed, dict):
        return json.dumps(parsed, default=str)
    dump_json = getattr(parsed, "model_dump_json", None)
    if callable(dump_json):
        try:
            out = dump_json()
            if isinstance(out, str) and out.strip():
                return out
        except Exception:
            pass
    dump = getattr(parsed, "model_dump", None)
    if callable(dump):
        try:
            return json.dumps(dump(), default=str)
        except Exception:
            pass
    try:
        return json.dumps(parsed, default=str)
    except TypeError:
        return None


def _text_from_generate_response(
    response: types.GenerateContentResponse,
    *,
    response_schema: dict[str, Any] | types.Schema | None,
) -> str:
    """Prefer ``response.text``; when empty in JSON/schema mode use ``response.parsed``."""
    text = response.text or ""
    if text.strip():
        return text
    if response_schema is None:
        return text
    parsed = getattr(response, "parsed", None)
    serialized = _parsed_to_json_text(parsed)
    return serialized if serialized else ""


def _log_empty_gemini_response(
    response: types.GenerateContentResponse,
    *,
    model_name: str,
    io_log_label: str,
) -> None:
    try:
        cand = response.candidates[0] if response.candidates else None
        finish = getattr(cand, "finish_reason", None) if cand else None
        block = getattr(response, "prompt_feedback", None)
        parts_info = ""
        if cand and cand.content and cand.content.parts:
            names: list[str] = []
            for p in cand.content.parts:
                d = p.model_dump(exclude_none=True)
                key = next((k for k, v in d.items() if v is not None and k != "thought"), "part")
                names.append(key)
            parts_info = repr(names[:12])
        logger.error(
            "Gemini empty text model=%s label=%s finish_reason=%s prompt_feedback=%s parts=%s",
            model_name,
            io_log_label,
            finish,
            block,
            parts_info or "none",
        )
    except Exception:
        logger.exception("Gemini empty body: logging detail failed")


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

    async def _generate_one_model(
        self,
        *,
        model_name: str,
        prompt: str,
        system_instruction: str | None,
        temperature: float,
        max_output_tokens: int,
        response_mime_type: str | None,
        response_schema: dict[str, Any] | types.Schema | None,
        io_log_label: str,
    ) -> AIResponse:
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
        response: types.GenerateContentResponse | None = None
        text = ""
        for attempt in range(1, settings.gemini_max_retries + 1):
            try:
                response = await self._client.aio.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=config,
                )
            except Exception as exc:
                last_error = exc
                logger.warning("Gemini API attempt %d failed: %s", attempt, exc)
                if attempt < settings.gemini_max_retries:
                    delay = _retry_delay_seconds(attempt, exc)
                    logger.info("Gemini retry sleep %.1fs before attempt %d", delay, attempt + 1)
                    await asyncio.sleep(delay)
                continue

            text = _text_from_generate_response(
                response,
                response_schema=response_schema,
            )
            if text.strip():
                break

            logger.warning(
                "Gemini returned empty body (attempt %d/%d) model=%s label=%s",
                attempt,
                settings.gemini_max_retries,
                model_name,
                io_log_label,
            )
            _log_empty_gemini_response(
                response,
                model_name=model_name,
                io_log_label=io_log_label,
            )
            last_error = RuntimeError("Gemini returned empty response body")
            if attempt < settings.gemini_max_retries:
                delay = _retry_delay_seconds(attempt, last_error)
                logger.info("Gemini empty-body retry sleep %.1fs", delay)
                await asyncio.sleep(delay)
        else:
            msg = f"Gemini API failed after {settings.gemini_max_retries} retries"
            raise RuntimeError(msg) from last_error

        elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
        usage = response.usage_metadata

        parsed_raw = getattr(response, "parsed", None)
        parsed_dict: dict[str, Any] | None = None
        if isinstance(parsed_raw, dict):
            parsed_dict = parsed_raw
        else:
            md = getattr(parsed_raw, "model_dump", None)
            if callable(md):
                try:
                    dumped = md()
                    if isinstance(dumped, dict):
                        parsed_dict = dumped
                except Exception:
                    pass

        ep, er = self._maybe_excerpts(prompt, text)
        self._maybe_log_io(
            label=io_log_label or "gemini",
            model=model_name,
            prompt=prompt,
            response_text=text,
        )

        return AIResponse(
            text=text,
            prompt_tokens=usage.prompt_token_count if usage else 0,
            completion_tokens=usage.candidates_token_count if usage else 0,
            total_tokens=usage.total_token_count if usage else 0,
            model=model_name,
            latency_ms=elapsed_ms,
            excerpt_prompt=ep,
            excerpt_response=er,
            parsed_dict=parsed_dict,
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
        explicit_model = model is not None
        primary = model if explicit_model else settings.gemini_pro_model
        chain: list[str] = [primary]
        if not explicit_model:
            fb = (settings.gemini_fallback_model or "").strip()
            if fb and fb != primary:
                chain.append(fb)

        last_runtime: RuntimeError | None = None
        for idx, model_name in enumerate(chain):
            try:
                return await self._generate_one_model(
                    model_name=model_name,
                    prompt=prompt,
                    system_instruction=system_instruction,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    response_mime_type=response_mime_type,
                    response_schema=response_schema,
                    io_log_label=io_log_label,
                )
            except RuntimeError as err:
                last_runtime = err
                inner = err.__cause__
                inner_exc = inner if isinstance(inner, BaseException) else None
                has_fallback = idx + 1 < len(chain)
                if has_fallback and is_transient_gemini_http_error(inner_exc):
                    logger.warning(
                        "Gemini model %s failed after retries; trying fallback %s",
                        model_name,
                        chain[idx + 1],
                    )
                    continue
                raise
        assert last_runtime is not None
        raise last_runtime

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
