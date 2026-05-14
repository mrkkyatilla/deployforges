"""Shared system prompt for JSON repair calls."""

REPAIR_JSON_SYSTEM = (
    "You receive text that was meant to be a single JSON object but may be invalid, "
    "truncated, or wrapped in markdown fences. Reply with ONE valid JSON object only — "
    "no prose, no markdown. Use exactly the keys requested in the user message."
)
