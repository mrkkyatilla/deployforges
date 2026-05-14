from __future__ import annotations

import json
import logging
import re
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
    schema_dockerfile_generation_metadata,
    schema_dockerfile_plan,
)
from core.ai.json_response import (
    ParseResult,
    mask_secrets,
    parse_model_json_from_ai_response,
    truncate_for_log,
)
from core.ai.prompts.dockerfile import (
    build_critic_prompt,
    build_dockerfile_body_only_prompt,
    build_fix_prompt,
    build_generation_metadata_prompt,
    build_generation_prompt,
    build_plan_prompt,
    build_refine_from_critic_prompt,
)
from core.ai.prompts.system import (
    DOCKERFILE_BODY_ONLY_SYSTEM_PROMPT,
    DOCKERFILE_CRITIC_SYSTEM_PROMPT,
    DOCKERFILE_PLAN_SYSTEM_PROMPT,
    DOCKERFILE_REFINE_FROM_CRITIC_SYSTEM_PROMPT,
    ERROR_FIX_SYSTEM_PROMPT,
    MASTER_DOCKERFILE_METADATA_SYSTEM_PROMPT,
    MASTER_SYSTEM_PROMPT,
)
from core.ai.templates import select_template
from core.ai.token_manager import (
    TokenBudget,
    fingerprint_is_high_complexity,
    select_generation_model,
    select_model_for_step,
)

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


_FROM_INSTRUCTION = re.compile(r"(?ms)^\s*FROM\s+\S", re.IGNORECASE)


def _dockerfile_is_plausible(content: str) -> bool:
    c = (content or "").lstrip()
    if len(c) < 8:
        return False
    return bool(_FROM_INSTRUCTION.match(c))


def _strip_markdown_fence_generic(text: str) -> str:
    """If the model wrapped output in ``` fences, extract inner body (any language tag)."""
    t = text.strip()
    pos = t.find("```")
    if pos < 0:
        return t
    after = t[pos + 3 :].lstrip()
    nl = after.find("\n")
    if nl >= 0:
        first = after[:nl].strip().lower()
        rest = after[nl + 1 :]
        if first and first not in ("", "```") and not first.startswith("```"):
            after = rest
    close = after.rfind("```")
    if close >= 0:
        after = after[:close]
    return after.strip()


def _try_json_dockerfile_field(text: str) -> str | None:
    """When ``text/plain`` still returns a JSON object with a ``dockerfile`` string."""
    s = text.strip()
    if not s.startswith("{"):
        return None
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    df = data.get("dockerfile")
    if isinstance(df, str) and df.strip():
        return df.strip()
    return None


def _normalize_plain_dockerfile_response(raw: str) -> str:
    """Strip BOM, markdown fences, accidental JSON wrapper, and preamble before the first FROM line."""
    t = (raw or "").replace("\ufeff", "").strip()
    if not t:
        return ""
    t = _strip_markdown_fence_generic(t)
    inner = _try_json_dockerfile_field(t)
    if inner:
        t = _strip_markdown_fence_generic(inner)
    m = _FROM_INSTRUCTION.search(t)
    if m:
        return t[m.start() :].strip()
    return t.strip()


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


def _generation_max_output_tokens(allowed: int, fingerprint: dict) -> int:
    """Align Pro max_output with budget and ``gemini_max_output_tokens_cap``; monorepo raises floor."""
    cap = int(settings.gemini_max_output_tokens_cap)
    is_mono = bool(fingerprint.get("is_monorepo")) or (
        fingerprint.get("monorepo_detection_method") == "multi_deps"
    )
    floor = (
        int(settings.ai_generation_output_floor_monorepo_tokens)
        if is_mono
        else int(settings.ai_generation_output_floor_tokens)
    )
    target = max(floor, min(allowed, cap))
    return min(cap, allowed, target)


def _critical_files_max_tokens(allowed: int, fingerprint: dict) -> int:
    if fingerprint_is_high_complexity(fingerprint):
        return min(5000, allowed // 2)
    return min(8000, allowed // 2)


def _user_hint_for_json_failures(
    pr: ParseResult,
    pr2: ParseResult | None,
    fingerprint: dict | None,
) -> str:
    fp = fingerprint or {}
    low = f"{pr.error or ''} {(pr2.error if pr2 else '') or ''}".lower()
    bits: list[str] = []
    if "unterminated string" in low:
        bits.append(
            "TR: JSON içinde kapanmamış dize (çoğunlukla Dockerfile gömülü). "
            "Repo bağlamını daraltıp tekrar deneyin veya iki aşamalı üretimi açık tutun (DF_AI_DOCKERFILE_TWO_PHASE_ENABLED). "
            "EN: Unterminated string in JSON; shrink context, retry, or keep two-phase generation enabled."
        )
    if pr.data is None and pr2 is None:
        bits.append(
            "TR: JSON onarımı çalıştırılamadı veya bütçe yetersiz. EN: JSON repair did not run or budget was too low."
        )
    elif pr2 is not None and pr2.data is None and pr2.error:
        bits.append(
            "TR: Flash onarım çıktısı ayrıştırılamadı. EN: Repair step returned unparseable JSON. "
            f"repair_parse={pr2.error}"
        )
    if fingerprint_is_high_complexity(fp):
        bits.append(
            "TR: Monorepo/karmaşık algı — kritik dosya bağlamı otomatik kısıldı. EN: High-complexity fingerprint; "
            "critical-file context was tightened automatically."
        )
    return " ".join(bits)


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
        parsed_dict: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, AIResponse | None]:
        return await repair_model_json(
            self.client,
            broken_text=broken_text,
            key_hint=key_hint,
            token_budget=token_budget,
            spend_step=spend_step,
            response_schema=response_schema,
            io_log_label=io_label,
            parsed_dict=parsed_dict,
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
        pr = parse_model_json_from_ai_response(response.text, response.parsed_dict)
        data = pr.data
        repair_resp: AIResponse | None = None
        if data is None and settings.ai_dockerfile_plan_json_repair_enabled:
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
                parsed_dict=response.parsed_dict,
            )
            if repaired is not None:
                if repair_resp is not None:
                    token_budget.record("dockerfile_plan", repair_resp.total_tokens)
                data = repaired
        elif data is None:
            logger.warning(
                "Dockerfile plan JSON parse failed; repair disabled "
                "(DF_AI_DOCKERFILE_PLAN_JSON_REPAIR_ENABLED=false) parse_error=%s",
                pr.error,
            )
        elif not _plan_is_usable(data):
            logger.warning(
                "Dockerfile plan JSON parsed but unusable; skipping LLM JSON repair",
            )
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

        ctx_cap = _critical_files_max_tokens(allowed, fingerprint)
        critical_files = self.context_builder.build_critical_files(
            project_path,
            max_tokens=ctx_cap,
            aggressive=fingerprint_is_high_complexity(fingerprint),
        )
        enriched_fp = {**fingerprint, "critical_files": critical_files}

        plan: dict[str, Any] | None = await self._generate_plan(enriched_fp, template, token_budget)

        if settings.ai_dockerfile_two_phase_enabled:
            return await self._generate_two_phase_dockerfile(
                enriched_fp=enriched_fp,
                template=template,
                plan=plan,
                token_budget=token_budget,
                allowed=allowed,
            )
        return await self._generate_single_json_dockerfile(
            enriched_fp=enriched_fp,
            template=template,
            plan=plan,
            token_budget=token_budget,
            allowed=allowed,
        )

    async def _generate_two_phase_dockerfile(
        self,
        *,
        enriched_fp: dict,
        template: str | None,
        plan: dict[str, Any] | None,
        token_budget: TokenBudget,
        allowed: int,
    ) -> DockerfileResult:
        meta_schema = schema_dockerfile_generation_metadata()
        max_meta_out = min(6144, _generation_max_output_tokens(allowed, enriched_fp))

        prompt_meta = build_generation_metadata_prompt(enriched_fp, template, plan=plan)
        resp_meta = await self.client.generate_json(
            prompt=prompt_meta,
            system_instruction=MASTER_DOCKERFILE_METADATA_SYSTEM_PROMPT,
            model=settings.gemini_flash_model,
            temperature=0.1,
            max_output_tokens=max_meta_out,
            response_schema=meta_schema,
            io_log_label="dockerfile_generation_metadata",
        )
        token_budget.record("generation_metadata", resp_meta.total_tokens)

        prm = parse_model_json_from_ai_response(resp_meta.text, resp_meta.parsed_dict)
        meta_data = prm.data
        repair_meta: AIResponse | None = None
        prm2: ParseResult | None = None
        if meta_data is None:
            logger.error("Generation metadata JSON parse failed: %s", prm.error)
            hint_m = (
                "analysis_summary (string), dockerignore (string), warnings (array of strings), "
                "exposed_ports (array of integers), optional estimated_image_size_mb (integer), "
                "optional requires_env_vars (array of strings)"
            )
            repaired_m, repair_meta = await self._repair_json_object(
                broken_text=resp_meta.text,
                key_hint=hint_m,
                token_budget=token_budget,
                spend_step="generation",
                response_schema=meta_schema,
                io_label="dockerfile_generation_metadata",
                parsed_dict=resp_meta.parsed_dict,
            )
            if repaired_m is not None:
                if repair_meta is not None:
                    token_budget.record("generation_metadata", repair_meta.total_tokens)
                    prm2 = parse_model_json_from_ai_response(
                        repair_meta.text, repair_meta.parsed_dict
                    )
                else:
                    prm2 = ParseResult(repaired_m, "local_json_recovery", resp_meta.text, None)
                meta_data = repaired_m

        if meta_data is None:
            hint_u = _user_hint_for_json_failures(prm, prm2, enriched_fp)
            repair_err = f"; repair_parse={prm2.error!s}" if prm2 is not None else ""
            raise RuntimeError(
                "AI Dockerfile metadata phase returned no valid JSON. "
                f"parse_error={prm.error!s}{repair_err}"
                + (f" | {hint_u}" if hint_u else "")
            )

        can2, allowed2 = token_budget.can_spend("generation", minimum=500)
        if not can2:
            raise RuntimeError(
                "Token budget exhausted after metadata phase (Dockerfile body step).",
            )

        max_body = _generation_max_output_tokens(allowed2, enriched_fp)
        prompt_body = build_dockerfile_body_only_prompt(enriched_fp, template, plan, meta_data)
        resp_body = await self.client.generate(
            prompt=prompt_body,
            system_instruction=DOCKERFILE_BODY_ONLY_SYSTEM_PROMPT,
            model=settings.gemini_pro_model,
            temperature=0.1,
            max_output_tokens=max_body,
            response_mime_type="text/plain",
            response_schema=None,
            io_log_label="dockerfile_generation_body",
        )
        token_budget.record("generation_body", resp_body.total_tokens)

        raw_body = resp_body.text or ""
        dockerfile = _normalize_plain_dockerfile_response(raw_body)
        if not _dockerfile_is_plausible(dockerfile):
            excerpt_n = max(400, min(2400, int(settings.ai_debug_max_chars)))
            excerpt = truncate_for_log(
                mask_secrets(raw_body, settings.secret_patterns),
                excerpt_n,
            )
            logger.warning(
                "Dockerfile body step failed plausible check raw_chars=%d normalized_chars=%d model=%s excerpt=%s",
                len(raw_body),
                len(dockerfile),
                resp_body.model,
                excerpt,
            )
            raise RuntimeError(
                "AI Dockerfile body phase returned empty or non-Dockerfile plain text. "
                "user_hint: TR: Dockerfile metni çıkmadı; tekrar deneyin. "
                "EN: No plausible Dockerfile from body step; retry or set DF_AI_DOCKERFILE_TWO_PHASE_ENABLED=false.",
            )

        full: dict[str, Any] = {**meta_data, "dockerfile": dockerfile}
        total_tok = (
            resp_meta.total_tokens
            + (repair_meta.total_tokens if repair_meta else 0)
            + resp_body.total_tokens
        )
        result = self._result_from_generation_dict(full, total_tok)
        meta_io = _io_meta(
            interaction="generation",
            parse_ok=True,
            parse_first=prm,
            parse_second=prm2,
            response=resp_meta,
            response2=repair_meta,
        )
        meta_io["two_phase"] = True
        meta_io["phase2_model"] = resp_body.model
        meta_io["phase2_tokens"] = resp_body.total_tokens
        meta_io["metadata_total_tokens"] = resp_meta.total_tokens + (
            repair_meta.total_tokens if repair_meta else 0
        )
        meta_io["metadata_prompt_tokens"] = resp_meta.prompt_tokens + (
            repair_meta.prompt_tokens if repair_meta else 0
        )
        meta_io["metadata_completion_tokens"] = resp_meta.completion_tokens + (
            repair_meta.completion_tokens if repair_meta else 0
        )
        meta_io["body_prompt_tokens"] = resp_body.prompt_tokens
        meta_io["body_completion_tokens"] = resp_body.completion_tokens
        meta_io["body_total_tokens"] = resp_body.total_tokens
        result.io_meta = meta_io
        _warn_if_sparse_comments(result.dockerfile)
        return result

    async def _generate_single_json_dockerfile(
        self,
        *,
        enriched_fp: dict,
        template: str | None,
        plan: dict[str, Any] | None,
        token_budget: TokenBudget,
        allowed: int,
    ) -> DockerfileResult:
        prompt = build_generation_prompt(enriched_fp, template, plan=plan)
        model = select_generation_model(enriched_fp)
        schema = schema_dockerfile_generation()
        max_out = _generation_max_output_tokens(allowed, enriched_fp)

        response = await self.client.generate_json(
            prompt=prompt,
            system_instruction=MASTER_SYSTEM_PROMPT,
            model=model,
            max_output_tokens=max_out,
            response_schema=schema,
            io_log_label="dockerfile_generation",
        )
        token_budget.record("generation", response.total_tokens)

        pr = parse_model_json_from_ai_response(response.text, response.parsed_dict)
        data = pr.data
        repair_resp: AIResponse | None = None
        pr2: ParseResult | None = None

        if data is None:
            logger.error("Generation JSON parse failed: %s", pr.error)
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
                parsed_dict=response.parsed_dict,
            )
            if repaired is not None:
                if repair_resp is not None:
                    token_budget.record("generation", repair_resp.total_tokens)
                    pr2 = parse_model_json_from_ai_response(
                        repair_resp.text, repair_resp.parsed_dict
                    )
                else:
                    pr2 = ParseResult(repaired, "local_json_recovery", response.text, None)
                data = repaired
        elif not _dockerfile_is_plausible(str(data.get("dockerfile", ""))):
            logger.error(
                "Generation produced empty or non-Dockerfile content (JSON parse OK); "
                "skipping LLM JSON repair",
            )

        if data is None or not _dockerfile_is_plausible(str(data.get("dockerfile", ""))):
            repair_err = f"; repair_parse={pr2.error!s}" if pr2 is not None else ""
            hint_line = _user_hint_for_json_failures(pr, pr2, enriched_fp)
            msg = (
                "AI Dockerfile generation returned no valid JSON Dockerfile after repair. "
                f"parse_error={pr.error!s}{repair_err}"
            )
            if hint_line:
                msg = f"{msg} | {hint_line}"
            raise RuntimeError(msg)

        total_tok = response.total_tokens + (repair_resp.total_tokens if repair_resp else 0)
        result = self._result_from_generation_dict(data, total_tok)
        result.io_meta = _io_meta(
            interaction="generation",
            parse_ok=True,
            parse_first=pr,
            parse_second=pr2,
            response=response,
            response2=repair_resp,
        )
        result.io_meta["two_phase"] = False
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
        pr = parse_model_json_from_ai_response(response.text, response.parsed_dict)
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
            project_path,
            max_tokens=_critical_files_max_tokens(allowed, fingerprint),
            aggressive=fingerprint_is_high_complexity(fingerprint),
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
            max_output_tokens=_generation_max_output_tokens(allowed, enriched_fp),
            response_schema=schema,
            io_log_label="dockerfile_refine",
        )
        token_budget.record("dockerfile_refine", response.total_tokens)
        pr = parse_model_json_from_ai_response(response.text, response.parsed_dict)
        data = pr.data
        repair_resp: AIResponse | None = None
        pr2: ParseResult | None = None
        if data is None:
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
                parsed_dict=response.parsed_dict,
            )
            if repaired is not None:
                if repair_resp is not None:
                    token_budget.record("dockerfile_refine", repair_resp.total_tokens)
                    pr2 = parse_model_json_from_ai_response(
                        repair_resp.text, repair_resp.parsed_dict
                    )
                else:
                    pr2 = ParseResult(repaired, "local_json_recovery", response.text, None)
                data = repaired
        elif not _dockerfile_is_plausible(str(data.get("dockerfile", ""))):
            logger.error(
                "Dockerfile refine produced empty or non-Dockerfile content (JSON parse OK); "
                "skipping LLM JSON repair",
            )
        if data is None or not _dockerfile_is_plausible(str(data.get("dockerfile", ""))):
            repair_err = f"; repair_parse={pr2.error!s}" if pr2 is not None else ""
            hint_line = _user_hint_for_json_failures(pr, pr2, enriched_fp)
            msg = (
                "AI Dockerfile refine-from-critic returned no valid JSON Dockerfile after repair. "
                f"parse_error={pr.error!s}{repair_err}"
            )
            if hint_line:
                msg = f"{msg} | {hint_line}"
            raise RuntimeError(msg)
        total_tok = response.total_tokens + (repair_resp.total_tokens if repair_resp else 0)
        result = self._result_from_generation_dict(data, total_tok)
        parse_second = pr2
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
            error_context=error_context[:10000],
            fingerprint=fingerprint,
            attempt_number=attempt_number,
        )
        model = select_model_for_step(step)
        schema = schema_dockerfile_fix()

        response = await self.client.generate_json(
            prompt=prompt,
            system_instruction=ERROR_FIX_SYSTEM_PROMPT,
            model=model,
            max_output_tokens=_generation_max_output_tokens(allowed, fingerprint or {}),
            response_schema=schema,
            io_log_label=f"dockerfile_fix_{attempt_number}",
        )

        token_budget.record(f"fix_attempt_{attempt_number}", response.total_tokens)

        pr = parse_model_json_from_ai_response(response.text, response.parsed_dict)
        data = pr.data
        repair_resp: AIResponse | None = None
        pr2: ParseResult | None = None

        if data is None:
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
                parsed_dict=response.parsed_dict,
            )
            if repaired is not None:
                if repair_resp is not None:
                    token_budget.record(f"fix_attempt_{attempt_number}", repair_resp.total_tokens)
                    pr2 = parse_model_json_from_ai_response(
                        repair_resp.text, repair_resp.parsed_dict
                    )
                else:
                    pr2 = ParseResult(repaired, "local_json_recovery", response.text, None)
                data = repaired
        elif not _dockerfile_is_plausible(str(data.get("dockerfile", ""))):
            logger.error(
                "Dockerfile fix produced empty or non-Dockerfile content (JSON parse OK); "
                "skipping LLM JSON repair",
            )

        if data is None or not _dockerfile_is_plausible(str(data.get("dockerfile", ""))):
            repair_err = f"; repair_parse={pr2.error!s}" if pr2 is not None else ""
            hint_line = _user_hint_for_json_failures(pr, pr2, fingerprint)
            msg = (
                f"AI Dockerfile fix returned no valid JSON Dockerfile (attempt {attempt_number}). "
                f"parse_error={pr.error!s}{repair_err}"
            )
            if hint_line:
                msg = f"{msg} | {hint_line}"
            raise RuntimeError(msg)

        total_tok = response.total_tokens + (repair_resp.total_tokens if repair_resp else 0)
        result = self._result_from_fix_dict(data, total_tok)
        parse_second = pr2
        result.io_meta = _io_meta(
            interaction=f"fix_attempt_{attempt_number}",
            parse_ok=True,
            parse_first=pr,
            parse_second=parse_second,
            response=response,
            response2=repair_resp,
        )
        return result
