"""One-off Gemini JSON repair (used by DockerfileGenerator and orchestrator)."""

from __future__ import annotations

import logging
from typing import Any

from api.config import settings
from core.ai.gemini_client import AIResponse, GeminiClient
from core.ai.json_response import parse_model_json_from_ai_response, parse_model_json_with_local_recovery
from core.ai.prompts.json_repair import REPAIR_JSON_SYSTEM

logger = logging.getLogger(__name__)


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
) -> tuple[dict[str, Any] | None, AIResponse | None]:
    """Try fast local JSON recovery; only if that fails, ask Flash for valid JSON.

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
        logger.warning("Token budget too low for JSON repair (%s)", spend_step)
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
    if pr.data is None:
        flash_local = parse_model_json_with_local_recovery(resp.text, resp.parsed_dict)
        if flash_local.data is not None:
            logger.info(
                "JSON repair recovered after Flash via local strategy=%s label=%s",
                flash_local.strategy,
                io_log_label,
            )
            return flash_local.data, resp
        logger.error("JSON repair still failed: %s", pr.error)
        return None, resp
    return pr.data, resp
