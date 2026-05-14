from __future__ import annotations

import logging
from dataclasses import dataclass, field

from api.config import settings

logger = logging.getLogger(__name__)

MODEL_COST_PER_MILLION = {
    "gemini-2.5-pro": {"input": 2.50, "output": 15.00},
    "gemini-2.5-flash": {"input": 0.30, "output": 3.50},
}

STEP_ALLOCATIONS = {
    "analysis": 0.10,
    "dockerfile_plan": 0.05,
    "generation": 0.22,
    "dockerfile_critic": 0.04,
    "dockerfile_refine": 0.08,
    "fix_attempt": 0.15,
    "compose": 0.10,
    "dockerignore": 0.05,
}

COMPLEXITY_THRESHOLDS = {
    "simple_fix": "gemini-2.5-flash",
    "dockerignore": "gemini-2.5-flash",
    "analysis_fallback": "gemini-2.5-flash",
    "dockerfile_plan": "gemini-2.5-flash",
    "dockerfile_critic": "gemini-2.5-flash",
    "dockerfile_refine": "gemini-2.5-pro",
    "generation": "gemini-2.5-pro",
    "complex_fix": "gemini-2.5-pro",
    "compose": "gemini-2.5-pro",
}


@dataclass
class TokenBudget:
    total: int = field(default_factory=lambda: settings.default_token_budget)
    spent: int = 0
    breakdown: dict[str, int] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.spent)

    def can_spend(self, step: str, minimum: int = 1000) -> tuple[bool, int]:
        step_budget = int(self.total * STEP_ALLOCATIONS.get(step, 0.10))
        allowed = min(step_budget, self.remaining)
        return allowed >= minimum, allowed

    def record(self, step: str, tokens: int) -> None:
        self.spent += tokens
        self.breakdown[step] = self.breakdown.get(step, 0) + tokens
        logger.info(
            "Token budget: spent %d/%d (+%d for %s), remaining %d",
            self.spent, self.total, tokens, step, self.remaining,
        )

    @property
    def cost_usd(self) -> float:
        total = 0.0
        for step, tokens in self.breakdown.items():
            model = select_model_for_step(step)
            costs = MODEL_COST_PER_MILLION.get(model, MODEL_COST_PER_MILLION["gemini-2.5-flash"])
            total += (tokens / 1_000_000) * (costs["input"] + costs["output"]) / 2
        return round(total, 6)


def select_model_for_step(step: str) -> str:
    return COMPLEXITY_THRESHOLDS.get(step, settings.gemini_pro_model)


def fingerprint_is_high_complexity(fingerprint: dict | None) -> bool:
    """Monorepo / multi-language signals: prefer Pro + tighter context budgets."""
    fp = fingerprint or {}
    if fp.get("is_monorepo"):
        return True
    if fp.get("monorepo_detection_method") == "multi_deps":
        return True
    services = fp.get("services")
    if isinstance(services, list) and len(services) > 2:
        return True
    secondary = (fp.get("language") or {}).get("secondary")
    if isinstance(secondary, list) and len(secondary) >= 2:
        return True
    return False


def select_generation_model(fingerprint: dict | None) -> str:
    """Model for Dockerfile JSON generation (legacy single-shot); two-phase uses Flash+Pro internally."""
    if fingerprint_is_high_complexity(fingerprint):
        return COMPLEXITY_THRESHOLDS.get("generation", settings.gemini_pro_model)
    if settings.ai_generation_use_flash_for_simple:
        return settings.gemini_flash_model
    return COMPLEXITY_THRESHOLDS.get("generation", settings.gemini_pro_model)


def estimate_tokens(text: str) -> int:
    return len(text) // 4
