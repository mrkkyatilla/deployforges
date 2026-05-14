from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from core.ai.dockerfile_generator import DockerfileGenerator
from core.ai.dockerfile_linter import DockerfileLinter
from core.ai.dockerfile_pipeline_policy import resolve_dockerfile_pipeline_policy
from core.ai.gemini_client import GeminiClient
from core.ai.token_manager import TokenBudget
from core.analysis.engine import AnalysisEngine
from core.intake.git_handler import GitHandler
from core.intake.archive_handler import ArchiveHandler
from core.intake.security_scan import SecurityScanner
from db.models import Build, Project
from db.session import async_session_factory

logger = logging.getLogger(__name__)


async def emit_event(project_id: UUID, event_type: str, data: dict) -> None:
    """Publish pipeline events to Redis for SSE streaming."""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        payload = json.dumps({"type": event_type, **data}, default=str)
        await r.publish(f"project:{project_id}:events", payload)
        await r.aclose()
    except Exception:
        logger.debug("Redis event publish failed (non-critical)", exc_info=True)


async def run_pipeline(project_id: UUID) -> None:
    """Main pipeline orchestrator — runs the full intake → analyze → generate → lint flow."""

    async with async_session_factory() as db:
        project = await db.get(Project, project_id)
        if not project:
            logger.error("Project %s not found", project_id)
            return

        project.status = "processing"
        await db.commit()

    token_budget = TokenBudget(total=settings.default_token_budget)

    try:
        # ── STAGE 1: INTAKE ──
        await emit_event(project_id, "step_start", {"step": "intake"})
        workspace = settings.workspace_base_path / str(project_id)
        workspace.mkdir(parents=True, exist_ok=True)

        async with async_session_factory() as db:
            project = await db.get(Project, project_id)

        if project.source_type == "git":
            handler = GitHandler()
            clone_result = await handler.clone(
                url=project.source_url,
                dest_path=workspace,
                branch=project.source_branch or "main",
                commit=project.source_commit,
            )
            if not clone_result.success:
                await _fail_project(project_id, f"Git clone failed: {clone_result.error_message}")
                return
        elif project.source_type in ("zip", "tar"):
            handler = ArchiveHandler()
            extract_result = await handler.extract(
                file_path=workspace / "upload",
                dest_path=workspace,
            )
            if not extract_result.success:
                await _fail_project(project_id, f"Extraction failed: {extract_result.error_message}")
                return

        await emit_event(project_id, "step_complete", {"step": "intake"})

        # ── STAGE 2: SECURITY SCAN ──
        await emit_event(project_id, "step_start", {"step": "security_scan"})
        scanner = SecurityScanner()
        scan_result = await scanner.scan(workspace)
        if not scan_result.is_safe:
            await _fail_project(
                project_id,
                f"Security scan failed: {scan_result.warnings[:3]}",
            )
            return
        await emit_event(project_id, "step_complete", {"step": "security_scan"})

        # ── STAGE 3: ANALYZE ──
        await emit_event(project_id, "step_start", {"step": "analyze"})
        engine = AnalysisEngine()
        fingerprint = await engine.analyze(str(workspace))
        fp_dict = fingerprint.to_dict()

        async with async_session_factory() as db:
            project = await db.get(Project, project_id)
            project.fingerprint = fp_dict
            await db.commit()

        await emit_event(project_id, "step_complete", {
            "step": "analyze",
            "result": {
                "language": fingerprint.language.primary,
                "framework": fingerprint.framework.name,
                "confidence": fingerprint.confidence,
            },
        })

        # ── STAGE 4: GENERATE DOCKERFILE ──
        await emit_event(project_id, "step_start", {"step": "generate_dockerfile"})
        gemini = GeminiClient()
        generator = DockerfileGenerator(gemini)
        fp = dict(fp_dict)
        fp["confidence"] = float(fingerprint.confidence)
        pipeline_policy = resolve_dockerfile_pipeline_policy(fp, settings)
        gen_result = await generator.generate(
            fingerprint=fp_dict,
            project_path=str(workspace),
            token_budget=token_budget,
            pipeline_policy=pipeline_policy,
        )

        await emit_event(project_id, "step_complete", {
            "step": "generate_dockerfile",
            "warnings": gen_result.warnings,
        })

        # ── STAGE 5: LINT ──
        await emit_event(project_id, "step_start", {"step": "lint_check"})
        linter = DockerfileLinter()
        lint_result = linter.lint(
            gen_result.dockerfile,
            port=fingerprint.port.value if fingerprint.port else None,
        )

        dockerfile = lint_result.fixed_dockerfile or gen_result.dockerfile
        dockerignore = gen_result.dockerignore

        lint_warnings = [issue.message for issue in lint_result.warnings]
        await emit_event(project_id, "step_complete", {
            "step": "lint_check",
            "is_valid": lint_result.is_valid,
            "warnings": lint_warnings,
        })

        # ── SAVE BUILD RECORD ──
        async with async_session_factory() as db:
            build = Build(
                project_id=project_id,
                attempt_number=1,
                dockerfile_content=dockerfile,
                dockerignore_content=dockerignore,
                build_status="generated",
                token_usage={
                    "total": token_budget.spent,
                    "breakdown": token_budget.breakdown,
                },
            )
            db.add(build)
            await db.commit()

        # ── FINALIZE (Faz 1: no build/deploy yet) ──
        all_warnings = gen_result.warnings + lint_warnings + fingerprint.warnings

        async with async_session_factory() as db:
            project = await db.get(Project, project_id)
            project.status = "success"
            project.final_dockerfile = dockerfile
            project.final_dockerignore = dockerignore
            project.total_tokens_used = token_budget.spent
            project.total_cost_usd = token_budget.cost_usd
            if all_warnings:
                project.error_summary = json.dumps(all_warnings[:10])
            await db.commit()

        await emit_event(project_id, "pipeline_complete", {
            "status": "success",
            "total_tokens": token_budget.spent,
            "cost_usd": token_budget.cost_usd,
        })

        logger.info(
            "Pipeline completed for project %s: tokens=%d, cost=$%.4f",
            project_id, token_budget.spent, token_budget.cost_usd,
        )

    except Exception as exc:
        logger.exception("Pipeline failed for project %s", project_id)
        await _fail_project(project_id, str(exc))
        await emit_event(project_id, "pipeline_complete", {
            "status": "failed",
            "error": str(exc),
        })


async def _fail_project(project_id: UUID, error: str) -> None:
    async with async_session_factory() as db:
        project = await db.get(Project, project_id)
        if project:
            project.status = "failed"
            project.error_summary = error[:2000]
            await db.commit()

    await emit_event(project_id, "pipeline_error", {"error": error})
    logger.error("Project %s failed: %s", project_id, error[:200])
