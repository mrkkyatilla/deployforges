"""One-off Gemini JSON repair (used by DockerfileGenerator and orchestrator)."""

from __future__ import annotations

import logging
from typing import Any

from api.config import settings
from core.ai.gemini_client import AIResponse, GeminiClient
from core.ai.json_response import (
    mask_secrets,
    parse_model_json_from_ai_response,
    parse_model_json_with_local_recovery,
    truncate_for_log,
)
from core.ai.prompts.json_repair import REPAIR_JSON_SYSTEM

logger = logging.getLogger(__name__)


def _masked_excerpt(text: str, max_chars: int = 2400) -> str:
    return truncate_for_log(
        mask_secrets(text or "", settings.secret_patterns),
        max_chars,
    )


def _merge_repair_responses(first: AIResponse, second: AIResponse) -> AIResponse:
    return AIResponse(
        text=second.text,
        prompt_tokens=first.prompt_tokens + second.prompt_tokens,
        completion_tokens=first.completion_tokens + second.completion_tokens,
        total_tokens=first.total_tokens + second.total_tokens,
        model=second.model,
        latency_ms=first.latency_ms + second.latency_ms,
        excerpt_prompt=second.excerpt_prompt or first.excerpt_prompt,
        excerpt_response=second.excerpt_response or first.excerpt_response,
        parsed_dict=second.parsed_dict,
    )


async def repair_model_json(
    client: GeminiClient,
    *,
    broken_text: str,
    key_hint: str,
    token_budget: Any,
    spend_step: str,
    response_schema: Any,
    io_log_label: str,
    parsed_dict: dict[str, Any] | None = None,
    second_attempt_enabled: bool | None = None,
) -> tuple[dict[str, Any] | None, AIResponse | None]:
    """Try fast local JSON recovery; only if that fails, ask Flash for valid JSON.

    ``second_attempt_enabled``: when ``None``, uses ``settings.ai_json_repair_second_attempt_enabled``.

    Returns ``(dict, None)`` when local recovery succeeds (no LLM call).
    """
    local_pr = parse_model_json_with_local_recovery(broken_text, parsed_dict)
    if local_pr.data is not None:
        logger.info(
            "JSON repair skipped (local recovery): strategy=%s label=%s",
            local_pr.strategy,
            io_log_label,
        )
        return local_pr.data, None

    can, allowed = token_budget.can_spend(spend_step)
    if not can or allowed < 512:
        logger.warning(
            "Token budget too low for JSON repair (%s); local_error=%s",
            spend_step,
            local_pr.error,
        )
        return None, AIResponse("", 0, 0, 0, settings.gemini_flash_model, 0)

    prompt = (
        f"Required JSON keys / shape: {key_hint}\n\n"
        "The model output below may include markdown or invalid JSON. "
        "Emit a single JSON object only.\n\n---\n"
        f"{broken_text[:12000]}"
    )
    resp = await client.generate_json(
        prompt=prompt,
        system_instruction=REPAIR_JSON_SYSTEM,
        model=settings.gemini_flash_model,
        max_output_tokens=min(8192, allowed),
        response_schema=response_schema,
        io_log_label=f"{io_log_label}_repair",
    )
    pr = parse_model_json_from_ai_response(resp.text, resp.parsed_dict)
    if pr.data is not None:
        return pr.data, resp

    flash_local = parse_model_json_with_local_recovery(resp.text, resp.parsed_dict)
    if flash_local.data is not None:
        logger.info(
            "JSON repair recovered after Flash via local strategy=%s label=%s",
            flash_local.strategy,
            io_log_label,
        )
        return flash_local.data, resp

    logger.error(
        "JSON repair Flash failed label=%s parse_error=%s flash_local_error=%s "
        "local_prerepair_error=%s broken_excerpt=%s flash_response_excerpt=%s",
        io_log_label,
        pr.error,
        flash_local.error,
        local_pr.error,
        _masked_excerpt(broken_text),
        _masked_excerpt(resp.text or ""),
    )

    allow_second = (
        settings.ai_json_repair_second_attempt_enabled
        if second_attempt_enabled is None
        else second_attempt_enabled
    )
    if not allow_second:
        return None, resp

    can2, allowed2 = token_budget.can_spend(spend_step)
    if not can2 or allowed2 < 400:
        logger.warning("JSON repair second attempt skipped (budget) label=%s", io_log_label)
        return None, resp

    tail = broken_text[-8000:] if len(broken_text) > 8000 else broken_text
    prompt2 = (
        "The JSON below may be truncated — the tail is most likely where the syntax broke. "
        f"Required keys/shape: {key_hint}\n"
        "Emit exactly one valid JSON object; no markdown.\n\n---\n"
        f"{tail}"
    )
    resp2 = await client.generate_json(
        prompt=prompt2,
        system_instruction=REPAIR_JSON_SYSTEM,
        model=settings.gemini_flash_model,
        max_output_tokens=min(8192, allowed2),
        response_schema=response_schema,
        io_log_label=f"{io_log_label}_repair2",
    )
    pr2 = parse_model_json_from_ai_response(resp2.text, resp2.parsed_dict)
    if pr2.data is not None:
        logger.info("JSON repair succeeded on second Flash attempt label=%s", io_log_label)
        return pr2.data, _merge_repair_responses(resp, resp2)

    flash_local2 = parse_model_json_with_local_recovery(resp2.text, resp2.parsed_dict)
    if flash_local2.data is not None:
        logger.info(
            "JSON repair second Flash recovered via local strategy=%s label=%s",
            flash_local2.strategy,
            io_log_label,
        )
        return flash_local2.data, _merge_repair_responses(resp, resp2)

    logger.error(
        "JSON repair second Flash failed label=%s parse_error=%s flash_local_error=%s excerpt=%s",
        io_log_label,
        pr2.error,
        flash_local2.error,
        _masked_excerpt(resp2.text or ""),
    )
    return None, _merge_repair_responses(resp, resp2)
