"""Build ``AIInteraction.extra`` JSON for optional DB persistence."""

from __future__ import annotations

from typing import Any

from api.config import settings
from core.ai.gemini_client import AIResponse


def build_ai_interaction_extra(
    *,
    io_meta: dict[str, Any] | None = None,
    response: AIResponse | None = None,
    parse_first: Any | None = None,
    parse_second: Any | None = None,
    repair_response: AIResponse | None = None,
) -> dict[str, Any] | None:
    """Return a dict for ``ai_interactions.extra`` when ``DF_AI_PERSIST_IO_EXCERPTS`` is true."""
    if not settings.ai_persist_io_excerpts:
        return None
    out: dict[str, Any] = {}
    if io_meta:
        out["io"] = io_meta
    if response:
        if response.excerpt_prompt:
            out["excerpt_prompt"] = response.excerpt_prompt
        if response.excerpt_response:
            out["excerpt_response"] = response.excerpt_response
    if repair_response:
        if repair_response.excerpt_prompt:
            out["excerpt_prompt_repair"] = repair_response.excerpt_prompt
        if repair_response.excerpt_response:
            out["excerpt_response_repair"] = repair_response.excerpt_response
    if parse_first is not None:
        out["parse_first"] = {
            "strategy": getattr(parse_first, "strategy", None),
            "error": getattr(parse_first, "error", None),
        }
    if parse_second is not None:
        out["parse_second"] = {
            "strategy": getattr(parse_second, "strategy", None),
            "error": getattr(parse_second, "error", None),
        }
    return out or None
