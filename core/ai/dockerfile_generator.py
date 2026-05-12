from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from core.ai.context_builder import ContextBuilder
from core.ai.gemini_client import AIResponse, GeminiClient
from core.ai.prompts.dockerfile import build_fix_prompt, build_generation_prompt
from core.ai.prompts.system import ERROR_FIX_SYSTEM_PROMPT, MASTER_SYSTEM_PROMPT
from core.ai.templates import select_template
from core.ai.token_manager import TokenBudget, select_model_for_step

logger = logging.getLogger(__name__)


@dataclass
class DockerfileResult:
    dockerfile: str
    dockerignore: str
    analysis_summary: str
    warnings: list[str] = field(default_factory=list)
    exposed_ports: list[int] = field(default_factory=list)
    estimated_image_size_mb: int | None = None
    requires_env_vars: list[str] = field(default_factory=list)
    tokens_used: int = 0


@dataclass
class FixResult:
    dockerfile: str
    dockerignore: str
    analysis_summary: str
    changes_made: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tokens_used: int = 0


class DockerfileGenerator:
    """Generates Dockerfiles using Gemini AI with template-based optimization."""

    def __init__(self, gemini_client: GeminiClient | None = None):
        self.client = gemini_client or GeminiClient()
        self.context_builder = ContextBuilder()

    async def generate(
        self,
        fingerprint: dict,
        project_path: str,
        token_budget: TokenBudget,
    ) -> DockerfileResult:
        can_spend, allowed = token_budget.can_spend("generation")
        if not can_spend:
            raise RuntimeError("Token budget exhausted for generation step")

        language = fingerprint.get("language", {}).get("primary", "python")
        template = select_template(language)

        critical_files = self.context_builder.build_critical_files(
            project_path, max_tokens=min(8000, allowed // 2)
        )
        enriched_fp = {**fingerprint, "critical_files": critical_files}

        prompt = build_generation_prompt(enriched_fp, template)
        model = select_model_for_step("generation")

        response = await self.client.generate_json(
            prompt=prompt,
            system_instruction=MASTER_SYSTEM_PROMPT,
            model=model,
            max_output_tokens=min(4096, allowed),
        )

        token_budget.record("generation", response.total_tokens)
        return self._parse_generation_response(response)

    async def fix(
        self,
        dockerfile: str,
        error_context: str,
        fingerprint: dict | None,
        attempt_number: int,
        token_budget: TokenBudget,
    ) -> FixResult:
        step = "simple_fix" if attempt_number <= 2 else "complex_fix"
        can_spend, allowed = token_budget.can_spend("fix_attempt")
        if not can_spend:
            raise RuntimeError("Token budget exhausted for fix step")

        prompt = build_fix_prompt(
            dockerfile=dockerfile,
            error_context=error_context[:3000],
            fingerprint=fingerprint,
            attempt_number=attempt_number,
        )
        model = select_model_for_step(step)

        response = await self.client.generate_json(
            prompt=prompt,
            system_instruction=ERROR_FIX_SYSTEM_PROMPT,
            model=model,
            max_output_tokens=min(4096, allowed),
        )

        token_budget.record(f"fix_attempt_{attempt_number}", response.total_tokens)
        return self._parse_fix_response(response)

    def _parse_generation_response(self, response: AIResponse) -> DockerfileResult:
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            logger.error("Failed to parse AI generation response as JSON")
            return DockerfileResult(
                dockerfile=response.text,
                dockerignore="",
                analysis_summary="Failed to parse structured response",
                warnings=["AI response was not valid JSON — raw text used as Dockerfile"],
                tokens_used=response.total_tokens,
            )

        return DockerfileResult(
            dockerfile=data.get("dockerfile", ""),
            dockerignore=data.get("dockerignore", ""),
            analysis_summary=data.get("analysis_summary", ""),
            warnings=data.get("warnings", []),
            exposed_ports=data.get("exposed_ports", []),
            estimated_image_size_mb=data.get("estimated_image_size_mb"),
            requires_env_vars=data.get("requires_env_vars", []),
            tokens_used=response.total_tokens,
        )

    def _parse_fix_response(self, response: AIResponse) -> FixResult:
        try:
            data = json.loads(response.text)
        except json.JSONDecodeError:
            logger.error("Failed to parse AI fix response as JSON")
            return FixResult(
                dockerfile=response.text,
                dockerignore="",
                analysis_summary="Failed to parse structured response",
                warnings=["AI response was not valid JSON"],
                tokens_used=response.total_tokens,
            )

        return FixResult(
            dockerfile=data.get("dockerfile", ""),
            dockerignore=data.get("dockerignore", ""),
            analysis_summary=data.get("analysis_summary", ""),
            changes_made=data.get("changes_made", []),
            warnings=data.get("warnings", []),
            tokens_used=response.total_tokens,
        )
