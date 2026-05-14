"""One-off Gemini JSON repair (used by DockerfileGenerator and orchestrator)."""

from __future__ import annotations

import logging
from typing import Any

from api.config import settings
from core.ai.gemini_client import AIResponse, GeminiClient
from core.ai.json_response import parse_model_json
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
) -> tuple[dict[str, Any] | None, AIResponse]:
    """Ask Flash to emit valid JSON matching ``response_schema``."""
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
        max_output_tokens=min(4096, allowed),
        response_schema=response_schema,
        io_log_label=f"{io_log_label}_repair",
    )
    pr = parse_model_json(resp.text)
    if pr.data is None:
        logger.error("JSON repair still failed: %s", pr.error)
        return None, resp
    return pr.data, resp
