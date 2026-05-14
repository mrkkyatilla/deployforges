from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from api.config import settings
from core.ai.context_builder import ContextBuilder
from core.ai.gemini_client import AIResponse, GeminiClient
from core.ai.gemini_json_repair import repair_model_json
from core.ai.gemini_schemas import (
    schema_dockerfile_critic,
    schema_dockerfile_fix,
    schema_dockerfile_generation,
    schema_dockerfile_plan,
)
from core.ai.json_response import parse_model_json
from core.ai.prompts.dockerfile import (
    build_critic_prompt,
    build_fix_prompt,
    build_generation_prompt,
    build_plan_prompt,
    build_refine_from_critic_prompt,
)
from core.ai.prompts.system import (
    DOCKERFILE_CRITIC_SYSTEM_PROMPT,
    DOCKERFILE_PLAN_SYSTEM_PROMPT,
    DOCKERFILE_REFINE_FROM_CRITIC_SYSTEM_PROMPT,
    ERROR_FIX_SYSTEM_PROMPT,
    MASTER_SYSTEM_PROMPT,
)
from core.ai.templates import select_template
from core.ai.token_manager import TokenBudget, select_model_for_step

logger = logging.getLogger(__name__)


def _io_meta(
    *,
    interaction: str,
    parse_ok: bool,
    parse_first: Any,
    parse_second: Any | None,
    response: AIResponse,
    response2: AIResponse | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "interaction": interaction,
        "parse_ok": parse_ok,
        "parse_first_strategy": getattr(parse_first, "strategy", ""),
        "parse_first_error": getattr(parse_first, "error", None),
        "excerpt_prompt": response.excerpt_prompt,
        "excerpt_response": response.excerpt_response,
    }
    if parse_second is not None:
        meta["parse_second_strategy"] = getattr(parse_second, "strategy", "")
        meta["parse_second_error"] = getattr(parse_second, "error", None)
    if response2 is not None:
        meta["excerpt_prompt_repair"] = response2.excerpt_prompt
        meta["excerpt_response_repair"] = response2.excerpt_response
    return meta


def _coerce_int_list(val: Any) -> list[int]:
    if not isinstance(val, list):
        return []
    out: list[int] = []
    for x in val:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out


def _coerce_str_list(val: Any) -> list[str]:
    if not isinstance(val, list):
        return []
    return [str(x) for x in val if x is not None]


def _dockerfile_is_plausible(content: str) -> bool:
    c = (content or "").lstrip()
    if len(c) < 8:
        return False
    return c.upper().startswith("FROM ")


def _plan_is_usable(plan: dict[str, Any] | None) -> bool:
    if not plan or not isinstance(plan, dict):
        return False
    bi = str(plan.get("base_image", "") or "").strip()
    stages = plan.get("stages")
    if not bi or not isinstance(stages, list) or len(stages) < 1:
        return False
    return True


def _warn_if_sparse_comments(dockerfile: str) -> None:
    lines = [ln.strip() for ln in (dockerfile or "").splitlines() if ln.strip()]
    if len(lines) < 4:
        return
    comment_lines = sum(1 for ln in lines if ln.startswith("#"))
    if comment_lines < 2:
        logger.info(
            "Dockerfile has few comment lines (%d); consider prompting review",
            comment_lines,
        )


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
    io_meta: dict[str, Any] | None = None


@dataclass
class FixResult:
    dockerfile: str
    dockerignore: str
    analysis_summary: str
    changes_made: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tokens_used: int = 0
    io_meta: dict[str, Any] | None = None


class DockerfileGenerator:
    """Generates Dockerfiles using Gemini AI with template-based optimization."""

    def __init__(self, gemini_client: GeminiClient | None = None):
        self.client = gemini_client or GeminiClient()
        self.context_builder = ContextBuilder()

    def _result_from_generation_dict(self, data: dict[str, Any], tokens: int) -> DockerfileResult:
        est = data.get("estimated_image_size_mb")
        if est is not None:
            try:
                est = int(float(est))
            except (TypeError, ValueError):
                est = None
        return DockerfileResult(
            dockerfile=str(data.get("dockerfile", "") or ""),
            dockerignore=str(data.get("dockerignore", "") or ""),
            analysis_summary=str(data.get("analysis_summary", "") or ""),
            warnings=_coerce_str_list(data.get("warnings")),
            exposed_ports=_coerce_int_list(data.get("exposed_ports")),
            estimated_image_size_mb=est,
            requires_env_vars=_coerce_str_list(data.get("requires_env_vars")),
            tokens_used=tokens,
        )

    def _result_from_fix_dict(self, data: dict[str, Any], tokens: int) -> FixResult:
        return FixResult(
            dockerfile=str(data.get("dockerfile", "") or ""),
            dockerignore=str(data.get("dockerignore", "") or ""),
            analysis_summary=str(data.get("analysis_summary", "") or ""),
            changes_made=_coerce_str_list(data.get("changes_made")),
            warnings=_coerce_str_list(data.get("warnings")),
            tokens_used=tokens,
        )

    async def _repair_json_object(
        self,
        *,
        broken_text: str,
        key_hint: str,
        token_budget: TokenBudget,
        spend_step: str,
        response_schema: Any,
        io_label: str,
    ) -> tuple[dict[str, Any] | None, AIResponse]:
        return await repair_model_json(
            self.client,
            broken_text=broken_text,
            key_hint=key_hint,
            token_budget=token_budget,
            spend_step=spend_step,
            response_schema=response_schema,
            io_log_label=io_label,
        )

    async def _generate_plan(
        self,
        fingerprint: dict,
        template: str | None,
        token_budget: TokenBudget,
    ) -> dict[str, Any] | None:
        if not settings.ai_dockerfile_plan_enabled:
            return None
        can_spend, allowed = token_budget.can_spend("dockerfile_plan", minimum=400)
        if not can_spend:
            return None
        schema = schema_dockerfile_plan()
        model = select_model_for_step("dockerfile_plan")
        prompt = build_plan_prompt(fingerprint, template)
        response = await self.client.generate_json(
            prompt=prompt,
            system_instruction=DOCKERFILE_PLAN_SYSTEM_PROMPT,
            model=model,
            max_output_tokens=min(2048, allowed),
            response_schema=schema,
            io_log_label="dockerfile_plan",
        )
        token_budget.record("dockerfile_plan", response.total_tokens)
        pr = parse_model_json(response.text)
        data = pr.data
        repair_resp: AIResponse | None = None
        if data is None or not _plan_is_usable(data):
            hint = (
                "base_image (string), stages (array of strings), copy_strategy (string), "
                "install_commands_outline (array of strings), cmd (string), "
                "optional healthcheck (string), non_root (boolean), optional notes (string)"
            )
            repaired, repair_resp = await self._repair_json_object(
                broken_text=response.text,
                key_hint=hint,
                token_budget=token_budget,
                spend_step="dockerfile_plan",
                response_schema=schema,
                io_label="dockerfile_plan",
            )
            if repaired is not None and repair_resp.text:
                token_budget.record("dockerfile_plan", repair_resp.total_tokens)
                data = repaired
        if data is None or not _plan_is_usable(data):
            logger.warning(
                "Dockerfile plan step failed or unusable; continuing without plan parse_error=%s",
                pr.error,
            )
            return None
        return data

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

        plan: dict[str, Any] | None = await self._generate_plan(enriched_fp, template, token_budget)

        prompt = build_generation_prompt(enriched_fp, template, plan=plan)
        model = select_model_for_step("generation")
        schema = schema_dockerfile_generation()

        response = await self.client.generate_json(
            prompt=prompt,
            system_instruction=MASTER_SYSTEM_PROMPT,
            model=model,
            max_output_tokens=min(4096, allowed),
            response_schema=schema,
            io_log_label="dockerfile_generation",
        )
        token_budget.record("generation", response.total_tokens)

        pr = parse_model_json(response.text)
        data = pr.data
        repair_resp: AIResponse | None = None
        pr2 = None

        if data is None or not _dockerfile_is_plausible(str(data.get("dockerfile", ""))):
            if data is None:
                logger.error("Generation JSON parse failed: %s", pr.error)
            else:
                logger.error("Generation produced empty or non-Dockerfile content")
            hint = (
                "analysis_summary (string), dockerfile (string), dockerignore (string), "
                "warnings (array of strings), exposed_ports (array of integers), "
                "optional estimated_image_size_mb (integer), "
                "optional requires_env_vars (array of strings)"
            )
            repaired, repair_resp = await self._repair_json_object(
                broken_text=response.text,
                key_hint=hint,
                token_budget=token_budget,
                spend_step="generation",
                response_schema=schema,
                io_label="dockerfile_generation",
            )
            if repaired is not None and repair_resp.text:
                token_budget.record("generation", repair_resp.total_tokens)
                pr2 = parse_model_json(repair_resp.text)
                data = repaired

        if data is None or not _dockerfile_is_plausible(str(data.get("dockerfile", ""))):
            repair_err = f"; repair_parse={pr2.error!s}" if pr2 is not None else ""
            raise RuntimeError(
                "AI Dockerfile generation returned no valid JSON Dockerfile after repair. "
                f"parse_error={pr.error!s}{repair_err}"
            )

        total_tok = response.total_tokens + (repair_resp.total_tokens if repair_resp else 0)
        result = self._result_from_generation_dict(data, total_tok)
        parse_second = pr2 if repair_resp is not None else None
        result.io_meta = _io_meta(
            interaction="generation",
            parse_ok=True,
            parse_first=pr,
            parse_second=parse_second,
            response=response,
            response2=repair_resp,
        )
        _warn_if_sparse_comments(result.dockerfile)
        return result

    async def run_critic(
        self,
        dockerfile: str,
        fingerprint: dict,
        token_budget: TokenBudget,
    ) -> dict[str, Any]:
        """Return {summary, issues} from model; empty issues if disabled or parse failure."""
        if not settings.ai_dockerfile_critic_refine_enabled:
            return {"summary": "", "issues": []}
        can_spend, allowed = token_budget.can_spend("dockerfile_critic", minimum=400)
        if not can_spend:
            return {"summary": "", "issues": []}
        schema = schema_dockerfile_critic()
        model = select_model_for_step("dockerfile_critic")
        prompt = build_critic_prompt(dockerfile, fingerprint)
        response = await self.client.generate_json(
            prompt=prompt,
            system_instruction=DOCKERFILE_CRITIC_SYSTEM_PROMPT,
            model=model,
            max_output_tokens=min(2048, allowed),
            response_schema=schema,
            io_log_label="dockerfile_critic",
        )
        token_budget.record("dockerfile_critic", response.total_tokens)
        pr = parse_model_json(response.text)
        data = pr.data
        if data is None:
            logger.warning("Dockerfile critic JSON parse failed: %s", pr.error)
            return {"summary": "critic_parse_failed", "issues": []}
        issues = data.get("issues")
        if not isinstance(issues, list):
            issues = []
        return {
            "summary": str(data.get("summary", "") or ""),
            "issues": [x for x in issues if isinstance(x, dict)],
        }

    async def refine_from_critic(
        self,
        *,
        dockerfile: str,
        dockerignore: str,
        critic: dict[str, Any],
        fingerprint: dict,
        project_path: str,
        token_budget: TokenBudget,
    ) -> DockerfileResult:
        can_spend, allowed = token_budget.can_spend("dockerfile_refine", minimum=500)
        if not can_spend:
            raise RuntimeError("Token budget exhausted for dockerfile_refine step")
        critical_files = self.context_builder.build_critical_files(
            project_path, max_tokens=min(6000, allowed // 2)
        )
        enriched_fp = {**fingerprint, "critical_files": critical_files}
        prompt = build_refine_from_critic_prompt(
            dockerfile, dockerignore, critic, enriched_fp
        )
        model = select_model_for_step("dockerfile_refine")
        schema = schema_dockerfile_generation()
        response = await self.client.generate_json(
            prompt=prompt,
            system_instruction=DOCKERFILE_REFINE_FROM_CRITIC_SYSTEM_PROMPT,
            model=model,
            max_output_tokens=min(4096, allowed),
            response_schema=schema,
            io_log_label="dockerfile_refine",
        )
        token_budget.record("dockerfile_refine", response.total_tokens)
        pr = parse_model_json(response.text)
        data = pr.data
        repair_resp: AIResponse | None = None
        pr2 = None
        if data is None or not _dockerfile_is_plausible(str(data.get("dockerfile", ""))):
            hint = (
                "analysis_summary (string), dockerfile (string), dockerignore (string), "
                "warnings (array of strings), exposed_ports (array of integers), "
                "optional estimated_image_size_mb (integer), "
                "optional requires_env_vars (array of strings)"
            )
            repaired, repair_resp = await self._repair_json_object(
                broken_text=response.text,
                key_hint=hint,
                token_budget=token_budget,
                spend_step="dockerfile_refine",
                response_schema=schema,
                io_label="dockerfile_refine",
            )
            if repaired is not None and repair_resp.text:
                token_budget.record("dockerfile_refine", repair_resp.total_tokens)
                pr2 = parse_model_json(repair_resp.text)
                data = repaired
        if data is None or not _dockerfile_is_plausible(str(data.get("dockerfile", ""))):
            raise RuntimeError(
                "AI Dockerfile refine-from-critic returned no valid JSON Dockerfile after repair. "
                f"parse_error={pr.error!s}"
            )
        total_tok = response.total_tokens + (repair_resp.total_tokens if repair_resp else 0)
        result = self._result_from_generation_dict(data, total_tok)
        parse_second = pr2 if repair_resp is not None else None
        result.io_meta = _io_meta(
            interaction="dockerfile_refine",
            parse_ok=True,
            parse_first=pr,
            parse_second=parse_second,
            response=response,
            response2=repair_resp,
        )
        _warn_if_sparse_comments(result.dockerfile)
        return result

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
        schema = schema_dockerfile_fix()

        response = await self.client.generate_json(
            prompt=prompt,
            system_instruction=ERROR_FIX_SYSTEM_PROMPT,
            model=model,
            max_output_tokens=min(4096, allowed),
            response_schema=schema,
            io_log_label=f"dockerfile_fix_{attempt_number}",
        )

        token_budget.record(f"fix_attempt_{attempt_number}", response.total_tokens)

        pr = parse_model_json(response.text)
        data = pr.data
        repair_resp: AIResponse | None = None
        pr2 = None

        if data is None or not _dockerfile_is_plausible(str(data.get("dockerfile", ""))):
            hint = (
                "analysis_summary (string), dockerfile (string), dockerignore (string), "
                "warnings (array of strings), changes_made (array of strings)"
            )
            repaired, repair_resp = await self._repair_json_object(
                broken_text=response.text,
                key_hint=hint,
                token_budget=token_budget,
                spend_step="fix_attempt",
                response_schema=schema,
                io_label=f"dockerfile_fix_{attempt_number}",
            )
            if repaired is not None and repair_resp.text:
                token_budget.record(f"fix_attempt_{attempt_number}", repair_resp.total_tokens)
                pr2 = parse_model_json(repair_resp.text)
                data = repaired

        if data is None or not _dockerfile_is_plausible(str(data.get("dockerfile", ""))):
            raise RuntimeError(
                f"AI Dockerfile fix returned no valid JSON Dockerfile (attempt {attempt_number}). "
                f"parse_error={pr.error!s}"
            )

        total_tok = response.total_tokens + (repair_resp.total_tokens if repair_resp else 0)
        result = self._result_from_fix_dict(data, total_tok)
        parse_second = pr2 if repair_resp is not None else None
        result.io_meta = _io_meta(
            interaction=f"fix_attempt_{attempt_number}",
            parse_ok=True,
            parse_first=pr,
            parse_second=parse_second,
            response=response,
            response2=repair_resp,
        )
        return result
