from __future__ import annotations

import dataclasses
import json
import logging
import operator
import time
import uuid
from pathlib import Path
from typing import Annotated, Any, TypedDict
from uuid import UUID

from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph

from api.config import settings
from core.ai.ai_interaction_extras import build_ai_interaction_extra
from core.ai.context_builder import ContextBuilder
from core.ai.dockerfile_generator import DockerfileGenerator, DockerfileResult, FixResult
from core.ai.dockerfile_linter import DockerfileLinter
from core.ai.gemini_client import GeminiClient, is_transient_gemini_http_error
from core.ai.pipeline_errors import TransientGeminiError
from core.ai.gemini_json_repair import repair_model_json
from core.ai.gemini_schemas import schema_ai_analysis
from core.ai.json_response import ParseResult, parse_model_json_from_ai_response
from core.ai.prompts.analysis import AI_ANALYSIS_SYSTEM_PROMPT, build_ai_analysis_prompt
from core.ai.token_manager import TokenBudget
from core.analysis.engine import AnalysisEngine
from core.analysis.fingerprint import ProjectFingerprint
from core.builder.sandbox import (
    BuildResult,
    DockerBuildSandbox,
    KanikoBuildSandbox,
    PreBuildValidator,
)
from core.builder.validator import CloudRunValidator
from core.error.classifier import BuildErrorClassifier, ClassifiedError
from core.error.parser import extract_error_lines
from core.intake.archive_handler import ArchiveHandler
from core.intake.git_handler import GitHandler
from core.intake.security_scan import SecurityScanner
from db.models import AIInteraction, Build, Project
from db.session import async_session_factory

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State definition
# ---------------------------------------------------------------------------

class DeployForgeState(TypedDict):
    # Input
    project_id: str
    project_path: str
    source_type: str
    source_url: str
    source_branch: str
    source_commit: str

    # Analysis
    fingerprint: dict
    analysis_confidence: float
    analysis_warnings: Annotated[list[str], operator.add]

    # Generation
    current_dockerfile: str
    current_dockerignore: str
    ai_warnings: Annotated[list[str], operator.add]
    lint_passed: bool
    dockerfile_critic: dict[str, Any]

    # Build
    build_attempts: Annotated[list[dict], operator.add]
    current_attempt: int
    max_attempts: int

    # Deployment / Testing
    deploy_url: str
    health_check_result: dict
    smoke_test_result: dict

    # Errors
    current_errors: list[dict]
    error_history: Annotated[list[dict], operator.add]

    # Token tracking
    total_tokens_used: int
    token_budget: int
    token_breakdown: dict

    # Final output
    final_status: str
    final_dockerfile: str
    final_dockerignore: str
    final_report: dict


# ---------------------------------------------------------------------------
# Redis event helper
# ---------------------------------------------------------------------------

async def _emit(project_id: str, event_type: str, data: dict) -> None:
    """Publish a pipeline event to the project's Redis channel."""
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        payload = json.dumps({"type": event_type, **data}, default=str)
        await client.publish(f"project:{project_id}:events", payload)
        await client.aclose()
    except Exception:
        logger.debug("Redis event publish failed (non-critical)", exc_info=True)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _tb_from_state(state: DeployForgeState) -> TokenBudget:
    """Reconstruct a TokenBudget from the flat state dict."""
    return TokenBudget(
        total=state.get("token_budget", settings.default_token_budget),
        spent=state.get("total_tokens_used", 0),
        breakdown=dict(state.get("token_breakdown", {})),
    )


def _error_to_dict(err: ClassifiedError) -> dict:
    return dataclasses.asdict(err)


def _failure_summary_from_final_report(final_report: Any) -> str | None:
    """Human-readable line for DB ``error_summary`` when ``current_errors`` is empty (e.g. intake)."""
    if not isinstance(final_report, dict) or not final_report:
        return None
    err = final_report.get("error")
    detail = final_report.get("detail")
    if err and detail is not None:
        if isinstance(detail, (dict, list)):
            detail_s = json.dumps(detail, default=str)
        else:
            detail_s = str(detail)
        return f"{err}: {detail_s[:900]}"
    if err:
        return str(err)
    return None


def _pipeline_crash_error_summary(exc: BaseException, *, max_len: int = 1800) -> str:
    """User-facing line when ``ainvoke`` raises (outside normal graph failure paths)."""
    parts: list[str] = []
    seen_ids: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and len(seen_ids) < 12:
        oid = id(cur)
        if oid in seen_ids:
            break
        seen_ids.add(oid)
        parts.append(str(cur))
        cur = cur.__cause__

    blob = " ".join(parts)
    low = blob.lower()
    if "503" in blob and ("unavailable" in low or "high demand" in low):
        return (
            "Gemini API temporarily unavailable (503, peak load). "
            "Wait a few minutes and retry the project."
        )
    if "429" in blob or "resource_exhausted" in low:
        return "Gemini API rate limit or quota exceeded. Retry later or check API billing."

    root = exc
    while root.__cause__ is not None:
        root = root.__cause__
    root_line = str(root).strip().split("\n")[0]
    if root_line:
        msg = f"Pipeline error: {root_line}"
        return msg if len(msg) <= max_len else msg[: max_len - 3] + "..."

    line = str(exc).strip().split("\n")[0]
    if line:
        return line if len(line) <= max_len else line[: max_len - 3] + "..."
    return "Internal pipeline error"


def _is_transient_gemini_crash(exc: BaseException) -> bool:
    """True when overload / quota errors suggest a Celery-level rerun may succeed."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if is_transient_gemini_http_error(cur):
            return True
        cur = cur.__cause__
    return False


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

async def intake_node(state: DeployForgeState) -> dict[str, Any]:
    """Git clone / archive extraction followed by a security pre-scan."""
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "intake"})

    project_path = state["project_path"]
    source_type = state["source_type"]
    dest = Path(project_path)
    dest.mkdir(parents=True, exist_ok=True)

    if source_type == "git":
        handler = GitHandler()
        result = await handler.clone(
            url=state.get("source_url", ""),
            dest_path=dest,
            branch=state.get("source_branch", "main") or "main",
            commit=state.get("source_commit") or None,
        )
        if not result.success:
            logger.error("Git clone failed for %s: %s", pid, result.error_message)
            await _emit(pid, "step_error", {"step": "intake", "error": result.error_message})
            return {
                "final_status": "failed",
                "final_report": {"error": "intake_failed", "detail": result.error_message},
            }
    elif source_type in ("zip", "tar", "tar.gz"):
        handler = ArchiveHandler()
        result = await handler.extract(
            file_path=dest / "upload",
            dest_path=dest,
        )
        if not result.success:
            logger.error("Archive extraction failed for %s: %s", pid, result.error_message)
            await _emit(pid, "step_error", {"step": "intake", "error": result.error_message})
            return {
                "final_status": "failed",
                "final_report": {"error": "intake_failed", "detail": result.error_message},
            }

    scanner = SecurityScanner()
    scan = await scanner.scan(dest)
    if not scan.is_safe:
        detail = {
            "secrets": scan.secrets_found[:5],
            "dangerous_files": scan.dangerous_files[:5],
            "suspicious": scan.suspicious_scripts[:5],
        }
        logger.warning("Security scan failed for %s: %s", pid, detail)
        await _emit(pid, "security_failed", {"detail": detail})
        return {
            "final_status": "failed",
            "final_report": {"error": "security_scan_failed", "detail": detail},
            "analysis_warnings": [f"Security threat detected: {scan.warnings[:3]}"],
        }

    await _emit(pid, "step_complete", {"step": "intake"})
    return {"project_path": str(dest)}


async def analyze_node(state: DeployForgeState) -> dict[str, Any]:
    """Deterministic project analysis via the AnalysisEngine."""
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "analyze"})

    engine = AnalysisEngine()
    fingerprint: ProjectFingerprint = await engine.analyze(state["project_path"])
    fp_dict = fingerprint.to_dict()

    await _emit(pid, "step_complete", {
        "step": "analyze",
        "language": fingerprint.language.primary,
        "framework": fingerprint.framework.name,
        "confidence": fingerprint.confidence,
    })

    return {
        "fingerprint": fp_dict,
        "analysis_confidence": fingerprint.confidence,
        "analysis_warnings": fingerprint.warnings,
    }


async def ai_analyze_node(state: DeployForgeState) -> dict[str, Any]:
    """AI-augmented analysis for low-confidence fingerprints (<0.7)."""
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "ai_analyze"})

    token_budget = _tb_from_state(state)
    can_spend, allowed = token_budget.can_spend("analysis")
    if not can_spend:
        logger.warning("Token budget too low for AI analysis, skipping")
        return {"analysis_warnings": ["Skipped AI analysis: token budget exhausted"]}

    ctx = ContextBuilder()
    file_tree = ctx.build_file_tree_list(state["project_path"])
    critical_files = ctx.build_critical_files(
        state["project_path"],
        max_tokens=min(8000, allowed // 2),
    )

    prompt = build_ai_analysis_prompt(file_tree, critical_files)
    client = GeminiClient()

    response = await client.generate_json(
        prompt=prompt,
        system_instruction=AI_ANALYSIS_SYSTEM_PROMPT,
        model=settings.gemini_flash_model,
        max_output_tokens=min(2048, allowed),
        response_schema=schema_ai_analysis(),
        io_log_label="ai_analysis",
    )

    token_budget.record("analysis", response.total_tokens)

    pr = parse_model_json_from_ai_response(response.text, response.parsed_dict)
    ai_result = pr.data
    repair_resp = None
    pr2 = None
    if ai_result is None:
        hint = (
            "language (string), optional language_version, framework, framework_version, "
            "entrypoint_file, start_command, port (integer), "
            "additional_system_deps (array of strings), "
            "warnings (array of strings), confidence (number)"
        )
        repaired, repair_resp = await repair_model_json(
            client,
            broken_text=response.text,
            key_hint=hint,
            token_budget=token_budget,
            spend_step="analysis",
            response_schema=schema_ai_analysis(),
            io_log_label="ai_analysis",
            parsed_dict=response.parsed_dict,
        )
        if repaired is not None:
            if repair_resp is not None:
                token_budget.record("analysis", repair_resp.total_tokens)
                pr2 = parse_model_json_from_ai_response(repair_resp.text, repair_resp.parsed_dict)
            else:
                pr2 = ParseResult(repaired, "local_json_recovery", response.text, None)
            ai_result = repaired

    extra = build_ai_interaction_extra(
        response=response,
        parse_first=pr,
        parse_second=pr2,
        repair_response=repair_resp,
    )

    # Persist the AI interaction
    try:
        async with async_session_factory() as db:
            db.add(AIInteraction(
                project_id=UUID(pid),
                interaction_type="ai_analysis",
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                model_used=response.model,
                latency_ms=response.latency_ms,
                extra=extra,
            ))
            await db.commit()
    except Exception:
        logger.warning("Failed to persist AI interaction record", exc_info=True)

    merged_fp = dict(state.get("fingerprint", {}))
    new_confidence = float(state.get("analysis_confidence", 0.5))
    ai_warnings: list[str] = []
    if isinstance(ai_result, dict):
        for key in (
            "language_version",
            "framework",
            "framework_version",
            "entrypoint_file",
            "start_command",
            "port",
            "additional_system_deps",
        ):
            if ai_result.get(key) is not None:
                merged_fp[key] = ai_result[key]
        new_confidence = float(ai_result.get("confidence", new_confidence))
        raw_warnings = ai_result.get("warnings", [])
        ai_warnings = raw_warnings if isinstance(raw_warnings, list) else []
    else:
        logger.error("Failed to parse AI analysis response after repair")
        ai_warnings = ["AI analysis response was not valid JSON"]

    await _emit(pid, "step_complete", {"step": "ai_analyze", "new_confidence": new_confidence})

    return {
        "fingerprint": merged_fp,
        "analysis_confidence": new_confidence,
        "analysis_warnings": ai_warnings,
        "total_tokens_used": token_budget.spent,
        "token_breakdown": token_budget.breakdown,
    }


async def generate_dockerfile_node(state: DeployForgeState) -> dict[str, Any]:
    """Generate a Dockerfile via the DockerfileGenerator."""
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "generate_dockerfile"})

    token_budget = _tb_from_state(state)
    client = GeminiClient()
    generator = DockerfileGenerator(client)

    gen_result: DockerfileResult = await generator.generate(
        fingerprint=state["fingerprint"],
        project_path=state["project_path"],
        token_budget=token_budget,
    )

    gen_extra = (
        build_ai_interaction_extra(io_meta=gen_result.io_meta)
        if gen_result.io_meta
        else None
    )

    try:
        async with async_session_factory() as db:
            db.add(AIInteraction(
                project_id=UUID(pid),
                interaction_type="generation",
                prompt_tokens=gen_result.tokens_used,
                completion_tokens=0,
                model_used=settings.gemini_pro_model,
                latency_ms=0,
                extra=gen_extra,
            ))
            await db.commit()
    except Exception:
        logger.warning("Failed to persist AI interaction record", exc_info=True)

    await _emit(
        pid,
        "step_complete",
        {"step": "generate_dockerfile", "warnings": gen_result.warnings},
    )

    return {
        "current_dockerfile": gen_result.dockerfile,
        "current_dockerignore": gen_result.dockerignore,
        "ai_warnings": gen_result.warnings,
        "dockerfile_critic": {},
        "total_tokens_used": token_budget.spent,
        "token_breakdown": token_budget.breakdown,
    }


async def dockerfile_critic_node(state: DeployForgeState) -> dict[str, Any]:
    """Optional static review of the generated Dockerfile (JSON issues only)."""
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "dockerfile_critic"})
    token_budget = _tb_from_state(state)
    client = GeminiClient()
    generator = DockerfileGenerator(client)
    spent_before = token_budget.breakdown.get("dockerfile_critic", 0)
    critic = await generator.run_critic(
        state["current_dockerfile"],
        state["fingerprint"],
        token_budget,
    )
    critic_tokens = max(0, token_budget.breakdown.get("dockerfile_critic", 0) - spent_before)
    critic_extra = None
    if settings.ai_persist_io_excerpts and critic_tokens > 0:
        critic_extra = {"interaction": "dockerfile_critic", "issue_count": len(critic.get("issues", []))}

    try:
        async with async_session_factory() as db:
            if critic_tokens > 0:
                db.add(AIInteraction(
                    project_id=UUID(pid),
                    interaction_type="dockerfile_critic",
                    prompt_tokens=critic_tokens,
                    completion_tokens=0,
                    model_used=settings.gemini_flash_model,
                    latency_ms=0,
                    extra=critic_extra,
                ))
                await db.commit()
    except Exception:
        logger.warning("Failed to persist dockerfile_critic interaction", exc_info=True)

    await _emit(
        pid,
        "step_complete",
        {
            "step": "dockerfile_critic",
            "issue_count": len(critic.get("issues", [])),
        },
    )
    return {
        "dockerfile_critic": critic,
        "total_tokens_used": token_budget.spent,
        "token_breakdown": token_budget.breakdown,
    }


async def dockerfile_refine_node(state: DeployForgeState) -> dict[str, Any]:
    """One full Dockerfile rewrite guided by critic issues (optional)."""
    pid = state["project_id"]
    critic = state.get("dockerfile_critic") or {}
    issues = critic.get("issues") or []
    if not settings.ai_dockerfile_critic_refine_enabled or not issues:
        await _emit(pid, "step_complete", {"step": "dockerfile_refine", "skipped": True})
        return {}

    await _emit(pid, "step_start", {"step": "dockerfile_refine"})
    token_budget = _tb_from_state(state)
    client = GeminiClient()
    generator = DockerfileGenerator(client)
    try:
        refine_result = await generator.refine_from_critic(
            dockerfile=state["current_dockerfile"],
            dockerignore=state["current_dockerignore"],
            critic=critic,
            fingerprint=state["fingerprint"],
            project_path=state["project_path"],
            token_budget=token_budget,
        )
    except Exception as exc:
        logger.exception("dockerfile_refine failed for project %s", pid)
        await _emit(pid, "step_complete", {
            "step": "dockerfile_refine",
            "error": str(exc),
        })
        return {
            "total_tokens_used": token_budget.spent,
            "token_breakdown": token_budget.breakdown,
            "ai_warnings": [f"Refine step failed (keeping previous Dockerfile): {exc!s}"],
        }

    refine_extra = (
        build_ai_interaction_extra(io_meta=refine_result.io_meta)
        if refine_result.io_meta
        else None
    )
    try:
        async with async_session_factory() as db:
            db.add(AIInteraction(
                project_id=UUID(pid),
                interaction_type="dockerfile_refine",
                prompt_tokens=refine_result.tokens_used,
                completion_tokens=0,
                model_used=settings.gemini_pro_model,
                latency_ms=0,
                extra=refine_extra,
            ))
            await db.commit()
    except Exception:
        logger.warning("Failed to persist dockerfile_refine interaction", exc_info=True)

    await _emit(pid, "step_complete", {"step": "dockerfile_refine"})
    return {
        "current_dockerfile": refine_result.dockerfile,
        "current_dockerignore": refine_result.dockerignore,
        "ai_warnings": refine_result.warnings,
        "total_tokens_used": token_budget.spent,
        "token_breakdown": token_budget.breakdown,
    }


def route_after_dockerfile_critic(state: DeployForgeState) -> str:
    if not settings.ai_dockerfile_critic_refine_enabled:
        return "to_lint"
    issues = (state.get("dockerfile_critic") or {}).get("issues") or []
    if not issues:
        return "to_lint"
    return "refine"


async def lint_check_node(state: DeployForgeState) -> dict[str, Any]:
    """Run deterministic lint checks on the current Dockerfile."""
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "lint_check"})

    linter = DockerfileLinter()
    fp = state.get("fingerprint", {})

    port_info = fp.get("port", {})
    port = port_info.get("value") if isinstance(port_info, dict) else None

    lint_result = linter.lint(state["current_dockerfile"], port=port)

    warnings = [i.message for i in lint_result.warnings]

    if lint_result.fixed_dockerfile and lint_result.is_valid:
        await _emit(pid, "step_complete", {
            "step": "lint_check",
            "auto_fixed": True,
            "warnings": warnings,
        })
        return {
            "lint_passed": True,
            "current_dockerfile": lint_result.fixed_dockerfile,
            "ai_warnings": warnings,
        }

    await _emit(pid, "step_complete", {
        "step": "lint_check",
        "is_valid": lint_result.is_valid,
        "errors": [i.message for i in lint_result.errors],
        "warnings": warnings,
    })
    return {"lint_passed": lint_result.is_valid, "ai_warnings": warnings}


async def auto_fix_lint_node(state: DeployForgeState) -> dict[str, Any]:
    """Apply the linter's auto-fix and mark lint as passed."""
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "auto_fix_lint"})

    linter = DockerfileLinter()
    fp = state.get("fingerprint", {})
    port_info = fp.get("port", {})
    port = port_info.get("value") if isinstance(port_info, dict) else None

    result = linter.lint(state["current_dockerfile"], port=port)

    fixed = result.fixed_dockerfile or state["current_dockerfile"]
    await _emit(pid, "step_complete", {"step": "auto_fix_lint"})
    return {"current_dockerfile": fixed, "lint_passed": True}


async def pre_build_validate_node(state: DeployForgeState) -> dict[str, Any]:
    """Static pre-build validation before kicking off the actual build."""
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "pre_build_validate"})

    validator = PreBuildValidator()
    result = await validator.validate(
        project_path=state["project_path"],
        dockerfile=state["current_dockerfile"],
    )

    if not result.can_build:
        err_details = [err for err in result.errors if err.is_error]
        logger.warning("Pre-build validation failed for %s: %s", pid, err_details)
        await _emit(pid, "step_error", {
            "step": "pre_build_validate",
            "errors": [
                {"name": err.name, "details": err.details, "is_error": err.is_error}
                for err in err_details
            ],
        })
        return {
            "current_errors": [
                {
                    "error_type": "pre_build",
                    "name": err.name,
                    "auto_fixable": False,
                    "severity": "high",
                    "fix_strategy": "unknown",
                    "match_text": (err.details or "")[:500],
                    "context": err.details or "",
                }
                for err in err_details
            ],
        }

    await _emit(pid, "step_complete", {"step": "pre_build_validate"})
    return {}


def _deploy_port_from_state(state: DeployForgeState) -> int:
    fp = state.get("fingerprint") or {}
    port_block = fp.get("port")
    if isinstance(port_block, dict):
        v = port_block.get("value")
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return 8080


async def build_node(state: DeployForgeState) -> dict[str, Any]:
    """Execute a Docker build inside a sandbox."""
    pid = state["project_id"]
    attempt = state.get("current_attempt", 0) + 1
    await _emit(pid, "step_start", {"step": "build", "attempt": attempt})
    start_ns = time.perf_counter_ns()

    build_id = f"{pid[:8]}-a{attempt}-{uuid.uuid4().hex[:6]}"
    backend = settings.build_backend

    if backend == "skip":
        result = BuildResult(
            success=True,
            image_ref=None,
            image_digest=None,
            logs="DF_BUILD_BACKEND=skip: image build skipped by configuration.",
            duration_ms=0,
        )
    elif backend == "kaniko":
        sandbox = KanikoBuildSandbox()
        result = await sandbox.build(
            project_path=state["project_path"],
            dockerfile_content=state["current_dockerfile"],
            build_id=build_id,
            dockerignore_content=state.get("current_dockerignore") or None,
        )
    else:
        sandbox = DockerBuildSandbox()
        result = await sandbox.build(
            project_path=state["project_path"],
            dockerfile_content=state["current_dockerfile"],
            build_id=build_id,
            dockerignore_content=state.get("current_dockerignore") or None,
        )

    combined_log = (result.logs or "") + (result.error_output or "")
    duration_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
    attempt_record = {
        "attempt": attempt,
        "success": result.success,
        "duration_ms": duration_ms,
        "image_digest": getattr(result, "image_digest", None),
        "image_ref": getattr(result, "image_ref", None),
        "log_tail": combined_log[-2000:],
    }

    try:
        async with async_session_factory() as db:
            build = Build(
                project_id=UUID(pid),
                attempt_number=attempt,
                dockerfile_content=state["current_dockerfile"],
                dockerignore_content=state.get("current_dockerignore", ""),
                build_log=combined_log or None,
                build_status="success" if result.success else "failed",
                image_digest=getattr(result, "image_digest", None),
                duration_ms=duration_ms,
                token_usage={
                    "total": state.get("total_tokens_used", 0),
                    "breakdown": state.get("token_breakdown", {}),
                },
            )
            db.add(build)
            await db.commit()
    except Exception:
        logger.warning("Failed to persist Build record", exc_info=True)

    await _emit(pid, "step_complete", {
        "step": "build",
        "attempt": attempt,
        "success": result.success,
        "duration_ms": duration_ms,
    })

    return {
        "current_attempt": attempt,
        "build_attempts": [attempt_record],
    }


async def deploy_and_test_node(state: DeployForgeState) -> dict[str, Any]:
    """Deploy the built image to Cloud Run and run health/smoke checks."""
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "deploy_and_test"})

    gcp = (settings.gcp_project_id or "").strip()
    if settings.build_backend == "skip" or not gcp:
        reason = "build_skip" if settings.build_backend == "skip" else "no_gcp"
        await _emit(pid, "step_complete", {
            "step": "deploy_and_test",
            "skipped": True,
            "reason": reason,
        })
        return {
            "deploy_url": "",
            "health_check_result": {
                "healthy": True,
                "status_code": None,
                "latency_ms": None,
                "skipped": True,
                "skip_reason": reason,
            },
            "smoke_test_result": {
                "passed": True,
                "details": f"Cloud Run deploy skipped ({reason}).",
            },
        }

    latest = state["build_attempts"][-1]
    image_ref = latest.get("image_digest") or latest.get("image_ref") or ""
    port = _deploy_port_from_state(state)

    validator = CloudRunValidator()
    result = await validator.validate(str(image_ref), port)

    health = result.health

    await _emit(pid, "step_complete", {
        "step": "deploy_and_test",
        "url": result.service_url or "",
        "healthy": health.healthy if health else False,
        "smoke_passed": result.success,
    })

    return {
        "deploy_url": result.service_url or "",
        "health_check_result": {
            "healthy": health.healthy if health else False,
            "status_code": health.status_code if health else None,
            "latency_ms": health.latency_ms if health else None,
        },
        "smoke_test_result": {
            "passed": result.success,
            "details": (
                "; ".join(f"{s.test.name}: {s.details}" for s in result.smoke_tests)
                if result.smoke_tests
                else (result.error or "")
            ),
        },
    }


async def classify_error_node(state: DeployForgeState) -> dict[str, Any]:
    """Classify the latest build/deploy errors for routing."""
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "classify_error"})

    pre_build_errors: list[dict] = state.get("current_errors", [])
    if pre_build_errors and pre_build_errors[0].get("error_type") == "pre_build":
        await _emit(pid, "step_complete", {"step": "classify_error", "source": "pre_build"})
        return {
            "current_errors": pre_build_errors,
            "error_history": pre_build_errors,
        }

    latest_attempt = state["build_attempts"][-1] if state.get("build_attempts") else {}
    build_log = latest_attempt.get("log_tail", "")

    fp = state.get("fingerprint", {})
    language = (
        fp.get("language", {}).get("primary")
        if isinstance(fp.get("language"), dict)
        else None
    )

    classifier = BuildErrorClassifier()
    classified = classifier.classify(build_log, language=language)

    if not classified:
        error_lines = extract_error_lines(build_log)
        generic = [{
            "error_type": "unknown",
            "name": "unclassified_build_error",
            "severity": "high",
            "auto_fixable": False,
            "fix_strategy": "ai_fix",
            "match_text": error_lines[:500],
            "context": error_lines,
        }]
        await _emit(pid, "step_complete", {"step": "classify_error", "count": 1, "type": "unknown"})
        return {"current_errors": generic, "error_history": generic}

    errors = [_error_to_dict(e) for e in classified]
    await _emit(pid, "step_complete", {
        "step": "classify_error",
        "count": len(errors),
        "auto_fixable": all(e["auto_fixable"] for e in errors),
    })
    return {"current_errors": errors, "error_history": errors}


async def auto_fix_build_node(state: DeployForgeState) -> dict[str, Any]:
    """Apply deterministic fixes for auto-fixable errors (missing deps, permissions)."""
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "auto_fix_build"})

    dockerfile = state["current_dockerfile"]
    errors = state.get("current_errors", [])

    for err in errors:
        strategy = err.get("fix_strategy", "")
        suggested = err.get("suggested_fix", "")

        if strategy == "add_system_package" and suggested:
            if "apt-get install" in dockerfile:
                dockerfile = dockerfile.replace(
                    "apt-get install -y --no-install-recommends",
                    f"apt-get install -y --no-install-recommends {suggested}",
                    1,
                )
            else:
                dockerfile = dockerfile.replace(
                    "WORKDIR /app",
                    f"RUN apt-get update && apt-get install -y --no-install-recommends {suggested} "
                    "&& rm -rf /var/lib/apt/lists/*\n\nWORKDIR /app",
                    1,
                )
        elif strategy == "fix_permissions":
            if "chmod" not in dockerfile:
                dockerfile = dockerfile.replace(
                    "USER ",
                    "RUN chmod -R 755 /app\nUSER ",
                    1,
                )
        elif strategy == "add_copy" and suggested:
            if "COPY . ." in dockerfile:
                dockerfile = dockerfile.replace("COPY . .", f"COPY {suggested}\nCOPY . .", 1)

    await _emit(pid, "step_complete", {"step": "auto_fix_build", "fixes_applied": len(errors)})
    return {"current_dockerfile": dockerfile, "current_errors": []}


async def ai_fix_build_node(state: DeployForgeState) -> dict[str, Any]:
    """Use Gemini to fix build errors that aren't auto-fixable."""
    pid = state["project_id"]
    attempt = state.get("current_attempt", 1)
    await _emit(pid, "step_start", {"step": "ai_fix_build", "attempt": attempt})

    token_budget = _tb_from_state(state)
    errors = state.get("current_errors", [])

    error_context = "\n---\n".join(
        f"[{e.get('name', 'unknown')}] {e.get('context', e.get('match_text', ''))}"
        for e in errors[:5]
    )

    client = GeminiClient()
    generator = DockerfileGenerator(client)
    fix_result: FixResult = await generator.fix(
        dockerfile=state["current_dockerfile"],
        error_context=error_context,
        fingerprint=state.get("fingerprint"),
        attempt_number=attempt,
        token_budget=token_budget,
    )

    fix_extra = (
        build_ai_interaction_extra(io_meta=fix_result.io_meta)
        if fix_result.io_meta
        else None
    )

    try:
        async with async_session_factory() as db:
            db.add(
                AIInteraction(
                    project_id=UUID(pid),
                    interaction_type=f"fix_attempt_{attempt}",
                    prompt_tokens=fix_result.tokens_used,
                    completion_tokens=0,
                    model_used=(
                        settings.gemini_flash_model
                        if attempt <= 2
                        else settings.gemini_pro_model
                    ),
                    latency_ms=0,
                    extra=fix_extra,
                )
            )
            await db.commit()
    except Exception:
        logger.warning("Failed to persist AI fix interaction record", exc_info=True)

    await _emit(pid, "step_complete", {
        "step": "ai_fix_build",
        "attempt": attempt,
        "changes": fix_result.changes_made,
    })

    updated_dockerignore = fix_result.dockerignore or state.get("current_dockerignore", "")
    return {
        "current_dockerfile": fix_result.dockerfile,
        "current_dockerignore": updated_dockerignore,
        "ai_warnings": fix_result.warnings,
        "current_errors": [],
        "total_tokens_used": token_budget.spent,
        "token_breakdown": token_budget.breakdown,
    }


async def finalize_success_node(state: DeployForgeState) -> dict[str, Any]:
    """Persist success status and emit the completion event."""
    pid = state["project_id"]

    token_budget = _tb_from_state(state)
    report = {
        "status": "success",
        "total_tokens": token_budget.spent,
        "cost_usd": token_budget.cost_usd,
        "attempts": len(state.get("build_attempts", [])),
        "deploy_url": state.get("deploy_url", ""),
    }

    try:
        async with async_session_factory() as db:
            project = await db.get(Project, UUID(pid))
            if project:
                project.status = "success"
                project.final_dockerfile = state["current_dockerfile"]
                project.final_dockerignore = state.get("current_dockerignore", "")
                project.total_tokens_used = token_budget.spent
                project.total_cost_usd = token_budget.cost_usd
                await db.commit()
    except Exception:
        logger.exception("Failed to persist success status for %s", pid)

    await _emit(pid, "pipeline_complete", report)
    logger.info("Pipeline succeeded for project %s: tokens=%d, cost=$%.4f",
                pid, token_budget.spent, token_budget.cost_usd)

    return {
        "final_status": "success",
        "final_dockerfile": state["current_dockerfile"],
        "final_dockerignore": state.get("current_dockerignore", ""),
        "final_report": report,
    }


async def finalize_failure_node(state: DeployForgeState) -> dict[str, Any]:
    """Persist failure status and emit the failure event."""
    pid = state["project_id"]

    token_budget = _tb_from_state(state)
    error_summary: list[str] = []
    for err in state.get("current_errors", []):
        error_summary.append(f"{err.get('name', 'unknown')}: {err.get('match_text', '')[:200]}")

    fr_line = _failure_summary_from_final_report(state.get("final_report"))
    if fr_line:
        error_summary.append(fr_line)

    if not error_summary:
        hist = state.get("error_history") or []
        for item in hist[-5:]:
            error_summary.append(str(item)[:300])

    report = {
        "status": "failed",
        "total_tokens": token_budget.spent,
        "cost_usd": token_budget.cost_usd,
        "attempts": len(state.get("build_attempts", [])),
        "errors": error_summary[:10],
        "error_history_count": len(state.get("error_history", [])),
    }

    try:
        async with async_session_factory() as db:
            project = await db.get(Project, UUID(pid))
            if project:
                project.status = "failed"
                project.error_summary = json.dumps(error_summary[:10], default=str)
                project.final_dockerfile = state.get("current_dockerfile", "")
                project.final_dockerignore = state.get("current_dockerignore", "")
                project.total_tokens_used = token_budget.spent
                project.total_cost_usd = token_budget.cost_usd
                await db.commit()
    except Exception:
        logger.exception("Failed to persist failure status for %s", pid)

    await _emit(pid, "pipeline_complete", report)
    logger.warning("Pipeline failed for project %s after %d attempts", pid, report["attempts"])

    return {
        "final_status": "failed",
        "final_dockerfile": state.get("current_dockerfile", ""),
        "final_dockerignore": state.get("current_dockerignore", ""),
        "final_report": report,
    }


# ---------------------------------------------------------------------------
# Routing functions
# ---------------------------------------------------------------------------

def route_after_intake(state: DeployForgeState) -> str:
    if state.get("final_status") == "failed":
        return "intake_failed"
    return "intake_ok"


def route_after_analysis(state: DeployForgeState) -> str:
    if state.get("analysis_confidence", 0.0) >= 0.7:
        return "high_confidence"
    return "low_confidence"


def route_after_lint(state: DeployForgeState) -> str:
    if state.get("lint_passed", False):
        return "lint_ok"
    return "lint_fail"


def route_after_pre_build(state: DeployForgeState) -> str:
    if state.get("current_errors"):
        return "validation_failed"
    return "validation_ok"


def route_after_build(state: DeployForgeState) -> str:
    attempts = state.get("build_attempts", [])
    if attempts and attempts[-1].get("success"):
        return "build_success"
    return "build_failure"


def route_after_deploy(state: DeployForgeState) -> str:
    hc = state.get("health_check_result", {})
    st = state.get("smoke_test_result", {})
    if hc.get("healthy") and st.get("passed"):
        return "deploy_success"
    return "deploy_failure"


def route_after_error_classification(state: DeployForgeState) -> str:
    if state.get("total_tokens_used", 0) >= state.get(
        "token_budget",
        settings.default_token_budget,
    ):
        return "token_budget_exceeded"

    if state.get("current_attempt", 0) >= state.get("max_attempts", settings.max_build_attempts):
        return "max_attempts"

    errors = state.get("current_errors", [])
    if errors and all(e.get("auto_fixable") for e in errors):
        return "auto_fixable"

    return "needs_ai"


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_deploy_graph() -> CompiledStateGraph:
    """Build and compile the DeployForge LangGraph state machine."""
    graph = StateGraph(DeployForgeState)

    # Nodes
    graph.add_node("intake", intake_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("ai_analyze", ai_analyze_node)
    graph.add_node("generate_dockerfile", generate_dockerfile_node)
    graph.add_node("dockerfile_critic", dockerfile_critic_node)
    graph.add_node("dockerfile_refine", dockerfile_refine_node)
    graph.add_node("lint_check", lint_check_node)
    graph.add_node("auto_fix_lint", auto_fix_lint_node)
    graph.add_node("pre_build_validate", pre_build_validate_node)
    graph.add_node("build", build_node)
    graph.add_node("deploy_and_test", deploy_and_test_node)
    graph.add_node("classify_error", classify_error_node)
    graph.add_node("auto_fix_build", auto_fix_build_node)
    graph.add_node("ai_fix_build", ai_fix_build_node)
    graph.add_node("finalize_success", finalize_success_node)
    graph.add_node("finalize_failure", finalize_failure_node)

    # Entry
    graph.set_entry_point("intake")

    # Intake → analyze (or fail fast if security/clone failed)
    graph.add_conditional_edges(
        "intake",
        route_after_intake,
        {
            "intake_ok": "analyze",
            "intake_failed": "finalize_failure",
        },
    )

    # Analyze → generate or AI-augment
    graph.add_conditional_edges(
        "analyze",
        route_after_analysis,
        {
            "high_confidence": "generate_dockerfile",
            "low_confidence": "ai_analyze",
        },
    )
    graph.add_edge("ai_analyze", "generate_dockerfile")

    graph.add_edge("generate_dockerfile", "dockerfile_critic")
    graph.add_conditional_edges(
        "dockerfile_critic",
        route_after_dockerfile_critic,
        {
            "refine": "dockerfile_refine",
            "to_lint": "lint_check",
        },
    )
    graph.add_edge("dockerfile_refine", "lint_check")

    # Lint → pre-build or auto-fix
    graph.add_conditional_edges(
        "lint_check",
        route_after_lint,
        {
            "lint_ok": "pre_build_validate",
            "lint_fail": "auto_fix_lint",
        },
    )
    graph.add_edge("auto_fix_lint", "pre_build_validate")

    # Pre-build validation → build or classify error
    graph.add_conditional_edges(
        "pre_build_validate",
        route_after_pre_build,
        {
            "validation_ok": "build",
            "validation_failed": "classify_error",
        },
    )

    # Build → deploy or classify error
    graph.add_conditional_edges(
        "build",
        route_after_build,
        {
            "build_success": "deploy_and_test",
            "build_failure": "classify_error",
        },
    )

    # Deploy → success or classify error
    graph.add_conditional_edges(
        "deploy_and_test",
        route_after_deploy,
        {
            "deploy_success": "finalize_success",
            "deploy_failure": "classify_error",
        },
    )

    # Error classification → auto-fix, AI fix, or give up
    graph.add_conditional_edges(
        "classify_error",
        route_after_error_classification,
        {
            "auto_fixable": "auto_fix_build",
            "needs_ai": "ai_fix_build",
            "max_attempts": "finalize_failure",
            "token_budget_exceeded": "finalize_failure",
        },
    )

    # After fixes → loop back
    graph.add_edge("auto_fix_build", "build")
    graph.add_edge("ai_fix_build", "lint_check")

    # Terminal nodes
    graph.add_edge("finalize_success", END)
    graph.add_edge("finalize_failure", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

async def run_pipeline(project_id: UUID) -> None:
    """Load a project from the DB, build initial state, and execute the graph."""
    async with async_session_factory() as db:
        project = await db.get(Project, project_id)
        if not project:
            logger.error("Project %s not found — cannot run pipeline", project_id)
            return

        project.status = "processing"
        await db.commit()

    workspace = settings.workspace_base_path / str(project_id)

    initial_state: DeployForgeState = {
        "project_id": str(project_id),
        "project_path": str(workspace),
        "source_type": project.source_type or "git",
        "source_url": project.source_url or "",
        "source_branch": project.source_branch or "main",
        "source_commit": project.source_commit or "",

        "fingerprint": {},
        "analysis_confidence": 0.0,
        "analysis_warnings": [],

        "current_dockerfile": "",
        "current_dockerignore": "",
        "ai_warnings": [],
        "lint_passed": False,
        "dockerfile_critic": {},

        "build_attempts": [],
        "current_attempt": 0,
        "max_attempts": settings.max_build_attempts,

        "deploy_url": "",
        "health_check_result": {},
        "smoke_test_result": {},

        "current_errors": [],
        "error_history": [],

        "total_tokens_used": 0,
        "token_budget": settings.default_token_budget,
        "token_breakdown": {},

        "final_status": "",
        "final_dockerfile": "",
        "final_dockerignore": "",
        "final_report": {},
    }

    compiled = build_deploy_graph()

    logger.info("Starting pipeline for project %s", project_id)
    start = time.perf_counter_ns()

    try:
        result = await compiled.ainvoke(initial_state)
    except Exception as exc:
        logger.exception("Unhandled error in pipeline for project %s", project_id)
        crash_summary = _pipeline_crash_error_summary(exc)
        if _is_transient_gemini_crash(exc):
            logger.warning(
                "Transient Gemini/provider error for project %s — propagating for Celery retry: %s",
                project_id,
                crash_summary,
            )
            raise TransientGeminiError(crash_summary) from exc

        try:
            async with async_session_factory() as db:
                project = await db.get(Project, project_id)
                if project:
                    project.status = "failed"
                    project.error_summary = crash_summary
                    await db.commit()
        except Exception:
            logger.exception("Failed to persist crash status for %s", project_id)

        await _emit(str(project_id), "pipeline_complete", {
            "status": "failed",
            "error": crash_summary,
        })
        return

    elapsed_ms = (time.perf_counter_ns() - start) // 1_000_000
    final_status = result.get("final_status", "unknown")
    logger.info(
        "Pipeline finished for project %s: status=%s, elapsed=%dms",
        project_id, final_status, elapsed_ms,
    )
