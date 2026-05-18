"""Multi-service LangGraph pipeline — DeploymentManifest v1 output."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

from langgraph.graph import END, StateGraph

from api.config import settings
from core.ai.compose_generator import ComposeGenerator
from core.ai.dockerfile_generator import DockerfileGenerator
from core.ai.dockerfile_linter import DockerfileLinter
from core.ai.dockerfile_pipeline_policy import resolve_dockerfile_pipeline_policy
from core.ai.gemini_client import GeminiClient
from core.ai.orchestrator import (
    DeployForgeState,
    _emit,
    _pipeline_crash_error_summary,
    _tb_from_state,
    ai_analyze_node,
    analyze_node,
    deploy_and_test_node,
    intake_node,
)
from core.ai.playbook_hints import collect_playbook_hints_for_prompt, record_playbook_hints_on_success
from core.ai.token_manager import TokenBudget
from core.analysis.engine import AnalysisEngine
from core.analysis.service_resolver import pick_primary, resolve_services
from core.builder.sandbox import BuildResult, DockerBuildSandbox, KanikoBuildSandbox
from core.builder.validator import CloudRunValidator
from core.manifest.builder import build_deployment_manifest
from core.security.dockerfile_policy import check_dockerfile_policy
from db.models import Build, Project
from db.session import async_session_factory

logger = logging.getLogger(__name__)


def _service_budget(total: TokenBudget, n_services: int) -> TokenBudget:
    n = max(1, n_services)
    per = max(5000, total.total // n)
    return TokenBudget(total=per, spent=total.spent, breakdown=dict(total.breakdown))


async def resolve_services_node(state: DeployForgeState) -> dict[str, Any]:
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "resolve_services"})

    engine = AnalysisEngine()
    root_fp = state.get("fingerprint") or {}
    file_tree = engine._build_file_tree(Path(state["project_path"]), [])  # noqa: SLF001

    resolved, build_order = resolve_services(
        state["project_path"],
        root_fp,
        file_tree=file_tree,
    )
    if len(resolved) > settings.max_services_per_project:
        resolved = resolved[: settings.max_services_per_project]
        build_order = build_order[: settings.max_services_per_project]

    primary = pick_primary(resolved)
    svc_dicts = [s.to_dict() for s in resolved]

    await _emit(pid, "step_complete", {
        "step": "resolve_services",
        "service_count": len(svc_dicts),
        "primary_service": primary,
    })

    return {
        "resolved_services": svc_dicts,
        "services_build_order": build_order,
        "primary_service": primary,
    }


async def analyze_services_node(state: DeployForgeState) -> dict[str, Any]:
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "analyze_services"})

    engine = AnalysisEngine()
    fingerprints: dict[str, dict] = {}
    warnings: list[str] = []

    for svc in state.get("resolved_services") or []:
        name = str(svc.get("name") or "app")
        root_path = str(svc.get("root_path") or ".")
        if svc.get("type") == "database":
            continue
        try:
            fp = await engine.analyze_service(
                state["project_path"],
                root_path,
                service_name=name,
            )
            fingerprints[name] = fp.to_dict()
            await _emit(pid, "service_complete", {
                "service_name": name,
                "step": "analyze_service",
            })
        except Exception as exc:
            logger.exception("Service analysis failed for %s", name)
            warnings.append(f"analysis_failed:{name}:{exc!s}")

    return {
        "service_fingerprints": fingerprints,
        "analysis_warnings": warnings,
    }


async def generate_services_node(state: DeployForgeState) -> dict[str, Any]:
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "generate_services"})

    token_budget = _tb_from_state(state)
    n = len([s for s in state.get("resolved_services") or [] if s.get("type") != "database"])
    svc_budget = _service_budget(token_budget, n)

    generator = DockerfileGenerator(GeminiClient())
    artifacts: dict[str, dict] = {}
    ai_warnings: list[str] = []

    order = state.get("services_build_order") or []
    by_name = {s["name"]: s for s in state.get("resolved_services") or [] if "name" in s}
    names = [n for n in order if n in by_name] + [
        s["name"] for s in state.get("resolved_services") or []
        if s.get("name") not in order and s.get("type") != "database"
    ]

    for name in names:
        svc = by_name.get(name)
        if not svc or svc.get("type") == "database":
            continue
        fp = (state.get("service_fingerprints") or {}).get(name)
        if not fp:
            ai_warnings.append(f"skip_generate_no_fingerprint:{name}")
            continue

        await _emit(pid, "service_start", {"service_name": name, "step": "generate_dockerfile"})
        root_path = str(svc.get("root_path") or ".")
        pol = resolve_dockerfile_pipeline_policy(fp, settings)
        hints = await collect_playbook_hints_for_prompt(fp)

        try:
            result = await generator.generate_for_service(
                repo_root=state["project_path"],
                service_root=root_path,
                service_fingerprint=fp,
                token_budget=svc_budget,
                pipeline_policy=pol,
                playbook_hints=hints,
            )
            method = "template"
            if result.io_meta and result.io_meta.get("template_first"):
                method = "template"
            elif result.tokens_used > 0:
                method = "llm"
            df_rel = f"{root_path.strip().lstrip('./')}/Dockerfile"
            artifacts[name] = {
                "dockerfile": result.dockerfile,
                "dockerignore": result.dockerignore,
                "generation_method": method,
                "dockerfile_path": df_rel,
                "lint_passed": False,
            }
            ai_warnings.extend(result.warnings)
        except Exception as exc:
            logger.exception("Generate failed for service %s", name)
            ai_warnings.append(f"generate_failed:{name}:{exc!s}")

        await _emit(pid, "service_complete", {"service_name": name, "step": "generate_dockerfile"})

    return {
        "service_artifacts": artifacts,
        "ai_warnings": ai_warnings,
        "total_tokens_used": token_budget.spent,
        "token_breakdown": token_budget.breakdown,
    }


async def generate_compose_node(state: DeployForgeState) -> dict[str, Any]:
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "generate_compose"})

    services = state.get("resolved_services") or []
    if len(services) < 2:
        return {}

    token_budget = _tb_from_state(state)
    gen = ComposeGenerator()
    fps = dict(state.get("service_fingerprints") or {})
    fps["__root__"] = state.get("fingerprint") or {}

    result = await gen.generate(
        services=services,
        fingerprints=fps,
        token_budget=token_budget,
    )

    return {
        "ai_warnings": result.warnings,
        "compose_yml": result.compose_yml,
        "total_tokens_used": token_budget.spent,
        "token_breakdown": token_budget.breakdown,
    }


async def build_manifest_node(state: DeployForgeState) -> dict[str, Any]:
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "build_manifest"})

    compose_yml = str(state.get("compose_yml") or "")

    manifest = build_deployment_manifest(
        root_fingerprint=state.get("fingerprint") or {},
        resolved_services=state.get("resolved_services") or [],
        service_fingerprints=state.get("service_fingerprints") or {},
        service_artifacts=state.get("service_artifacts") or {},
        compose_yml=compose_yml,
        levels_passed=["L0_analysis"],
        warnings=list(state.get("analysis_warnings") or []) + list(state.get("ai_warnings") or []),
        source_type=state.get("source_type"),
    )

    primary = state.get("primary_service") or manifest.validation.primary_service
    artifacts = state.get("service_artifacts") or {}
    if primary and primary in artifacts:
        current_df = artifacts[primary].get("dockerfile", "")
        current_ign = artifacts[primary].get("dockerignore", "")
    elif artifacts:
        first = next(iter(artifacts.values()))
        current_df = first.get("dockerfile", "")
        current_ign = first.get("dockerignore", "")
    else:
        current_df = ""
        current_ign = ""

    return {
        "deployment_manifest": manifest.to_dict(),
        "current_dockerfile": current_df,
        "current_dockerignore": current_ign,
    }


async def lint_all_services_node(state: DeployForgeState) -> dict[str, Any]:
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "lint_all_services"})

    linter = DockerfileLinter()
    artifacts = dict(state.get("service_artifacts") or {})
    levels = list((state.get("deployment_manifest") or {}).get("validation", {}).get("levels_passed", []))
    if not isinstance(levels, list):
        levels = []
    all_ok = True

    for name, art in artifacts.items():
        fp = (state.get("service_fingerprints") or {}).get(name, {})
        port_block = fp.get("port") if isinstance(fp, dict) else {}
        port = 8080
        if isinstance(port_block, dict) and port_block.get("value"):
            try:
                port = int(port_block["value"])
            except (TypeError, ValueError):
                pass

        lint_result = linter.lint(str(art.get("dockerfile") or ""), port=port)
        if lint_result.fixed_dockerfile and lint_result.is_valid:
            art["dockerfile"] = lint_result.fixed_dockerfile
        art["lint_passed"] = lint_result.is_valid
        if check_dockerfile_policy(art["dockerfile"]):
            art["lint_passed"] = False
        if not art["lint_passed"]:
            all_ok = False

    if all_ok and artifacts:
        levels = list(set(levels + ["L1"]))

    dm = dict(state.get("deployment_manifest") or {})
    val = dict(dm.get("validation") or {})
    val["levels_passed"] = levels
    dm["validation"] = val

    return {
        "service_artifacts": artifacts,
        "deployment_manifest": dm,
        "lint_passed": all_ok,
    }


async def _build_one_service(
    state: DeployForgeState,
    name: str,
    art: dict,
    svc: dict,
) -> tuple[str, BuildResult]:
    build_id = f"{state['project_id'][:8]}-{name}-{uuid.uuid4().hex[:6]}"
    root_path = str(svc.get("root_path") or ".")
    df_rel = art.get("dockerfile_path") or f"{root_path.strip().lstrip('./')}/Dockerfile"

    if settings.build_backend == "kaniko":
        sandbox = KanikoBuildSandbox()
    else:
        sandbox = DockerBuildSandbox()

    result = await sandbox.build(
        project_path=state["project_path"],
        dockerfile_content=art.get("dockerfile", ""),
        build_id=build_id,
        dockerignore_content=art.get("dockerignore"),
        context_path=state["project_path"],
        dockerfile_rel=df_rel,
    )
    return name, result


async def build_services_l2_node(state: DeployForgeState) -> dict[str, Any]:
    pid = state["project_id"]
    await _emit(pid, "step_start", {"step": "build_services_l2"})

    if settings.build_backend == "skip":
        dm = dict(state.get("deployment_manifest") or {})
        val = dict(dm.get("validation") or {})
        lp = val.get("levels_passed") or []
        if isinstance(lp, list):
            val["levels_passed"] = list(set(lp + ["L2_skipped"]))
        dm["validation"] = val
        return {"deployment_manifest": dm}

    by_name = {s["name"]: s for s in state.get("resolved_services") or [] if "name" in s}
    artifacts = state.get("service_artifacts") or {}
    tasks = []

    for name, art in artifacts.items():
        svc = by_name.get(name)
        if not svc:
            continue
        stype = svc.get("type")
        if stype == "database":
            continue
        if stype == "worker" and not settings.validate_worker_build:
            continue
        tasks.append(_build_one_service(state, name, art, svc))

    results: dict[str, BuildResult] = {}
    if tasks:
        done = await asyncio.gather(*tasks, return_exceptions=True)
        for item in done:
            if isinstance(item, Exception):
                logger.exception("L2 build task failed", exc_info=item)
                continue
            name, br = item
            results[name] = br

    l2_ok = all(r.success for r in results.values()) if results else True
    attempts = list(state.get("build_attempts") or [])

    try:
        async with async_session_factory() as db:
            for name, br in results.items():
                art = artifacts.get(name, {})
                combined = (br.logs or "") + (br.error_output or "")
                db.add(Build(
                    project_id=UUID(pid),
                    service_name=name,
                    attempt_number=len(attempts) + 1,
                    dockerfile_content=art.get("dockerfile", ""),
                    dockerignore_content=art.get("dockerignore", ""),
                    build_log=combined or None,
                    build_status="success" if br.success else "failed",
                    image_digest=getattr(br, "image_digest", None),
                    duration_ms=br.duration_ms,
                ))
                attempts.append({
                    "service": name,
                    "success": br.success,
                    "duration_ms": br.duration_ms,
                })
            await db.commit()
    except Exception:
        logger.warning("Failed to persist multi-service builds", exc_info=True)

    dm = dict(state.get("deployment_manifest") or {})
    val = dict(dm.get("validation") or {})
    lp = val.get("levels_passed") or []
    if isinstance(lp, list) and l2_ok:
        val["levels_passed"] = list(set(lp + ["L2"]))
    dm["validation"] = val

    if not l2_ok:
        return {
            "deployment_manifest": dm,
            "build_attempts": attempts,
            "final_status": "partial" if artifacts else "failed",
            "current_errors": [{
                "error_type": "build",
                "name": "multi_service_l2_failed",
                "auto_fixable": False,
                "severity": "high",
                "fix_strategy": "unknown",
                "match_text": "One or more service builds failed",
                "context": "",
            }],
        }

    return {
        "deployment_manifest": dm,
        "build_attempts": attempts,
    }


def route_after_l2(state: DeployForgeState) -> str:
    if state.get("current_errors"):
        primary = state.get("primary_service")
        if state.get("final_status") == "partial" and primary:
            return "deploy_primary"
        return "finalize_failure"
    return "deploy_primary"


async def finalize_multi_success_node(state: DeployForgeState) -> dict[str, Any]:
    pid = state["project_id"]
    token_budget = _tb_from_state(state)
    dm = dict(state.get("deployment_manifest") or {})
    val = dict(dm.get("validation") or {})
    if state.get("deploy_url"):
        val["deploy_url"] = state["deploy_url"]
        val["cloud_run_service"] = state.get("primary_service")
        lp = val.get("levels_passed") or []
        if isinstance(lp, list):
            val["levels_passed"] = list(set(lp + ["L3"]))
    dm["validation"] = val

    compose_yml = str((dm.get("artifacts") or {}).get("compose_yml") or state.get("compose_yml") or "")

    status = state.get("final_status") or "success"
    if status not in ("success", "partial"):
        status = "success"

    try:
        async with async_session_factory() as db:
            project = await db.get(Project, UUID(pid))
            if project:
                project.status = status
                project.final_manifest = dm
                project.manifest_version = "1"
                project.final_dockerfile = state.get("current_dockerfile", "")
                project.final_dockerignore = state.get("current_dockerignore", "")
                project.final_compose = compose_yml or project.final_compose
                project.total_tokens_used = token_budget.spent
                project.total_cost_usd = token_budget.cost_usd
                await db.commit()
    except Exception:
        logger.exception("Failed to persist multi-service success for %s", pid)

    await record_playbook_hints_on_success(
        state.get("fingerprint"),
        list(state.get("error_history") or []),
    )
    await _emit(pid, "manifest_ready", {"status": status})
    await _emit(pid, "pipeline_complete", {"status": status, "primary_service": state.get("primary_service")})

    return {
        "final_status": status,
        "deployment_manifest": dm,
        "final_dockerfile": state.get("current_dockerfile", ""),
        "final_dockerignore": state.get("current_dockerignore", ""),
        "final_report": {"status": status, "total_tokens": token_budget.spent},
    }


async def finalize_multi_failure_node(state: DeployForgeState) -> dict[str, Any]:
    pid = state["project_id"]
    token_budget = _tb_from_state(state)
    dm = state.get("deployment_manifest") or {}

    try:
        async with async_session_factory() as db:
            project = await db.get(Project, UUID(pid))
            if project:
                project.status = "failed"
                if dm:
                    project.final_manifest = dm
                project.final_dockerfile = state.get("current_dockerfile", "")
                project.total_tokens_used = token_budget.spent
                project.total_cost_usd = token_budget.cost_usd
                await db.commit()
    except Exception:
        logger.exception("Failed to persist multi-service failure for %s", pid)

    await _emit(pid, "pipeline_complete", {"status": "failed"})
    return {"final_status": "failed", "deployment_manifest": dm}


def build_multi_service_graph():
    graph = StateGraph(DeployForgeState)

    graph.add_node("intake", intake_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("ai_analyze", ai_analyze_node)
    graph.add_node("resolve_services", resolve_services_node)
    graph.add_node("analyze_services", analyze_services_node)
    graph.add_node("generate_services", generate_services_node)
    graph.add_node("generate_compose", generate_compose_node)
    graph.add_node("build_manifest", build_manifest_node)
    graph.add_node("lint_all", lint_all_services_node)
    graph.add_node("build_l2", build_services_l2_node)
    graph.add_node("deploy_primary", deploy_and_test_node)
    graph.add_node("finalize_success", finalize_multi_success_node)
    graph.add_node("finalize_failure", finalize_multi_failure_node)

    graph.set_entry_point("intake")

    def route_intake(s: DeployForgeState) -> str:
        return "failed" if s.get("final_status") == "failed" else "ok"

    graph.add_conditional_edges("intake", route_intake, {"ok": "analyze", "failed": "finalize_failure"})
    graph.add_conditional_edges(
        "analyze",
        lambda s: "ai" if float(s.get("analysis_confidence", 0)) < 0.7 else "ok",
        {"ai": "ai_analyze", "ok": "resolve_services"},
    )
    graph.add_edge("ai_analyze", "resolve_services")
    graph.add_edge("resolve_services", "analyze_services")
    graph.add_edge("analyze_services", "generate_services")
    graph.add_edge("generate_services", "generate_compose")
    graph.add_edge("generate_compose", "build_manifest")
    graph.add_edge("build_manifest", "lint_all")
    graph.add_edge("lint_all", "build_l2")
    graph.add_conditional_edges(
        "build_l2",
        route_after_l2,
        {"deploy_primary": "deploy_primary", "finalize_failure": "finalize_failure"},
    )
    graph.add_conditional_edges(
        "deploy_primary",
        lambda s: "ok" if not s.get("current_errors") else "fail",
        {"ok": "finalize_success", "fail": "finalize_failure"},
    )
    graph.add_edge("finalize_success", END)
    graph.add_edge("finalize_failure", END)

    return graph.compile()


async def run_multi_service_pipeline(project_id: UUID) -> None:
    """Execute multi-service graph (manifest v1)."""
    async with async_session_factory() as db:
        project = await db.get(Project, project_id)
        if not project:
            logger.error("Project %s not found", project_id)
            return
        project.status = "processing"
        await db.commit()

    workspace = settings.workspace_base_path / str(project_id)

    initial: DeployForgeState = {
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
        "dockerfile_pipeline_policy": {},
        "resolved_services": [],
        "service_fingerprints": {},
        "service_artifacts": {},
        "services_build_order": [],
        "primary_service": None,
        "deployment_manifest": {},
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
        "pipeline_step_timings": [],
        "final_status": "",
        "final_dockerfile": "",
        "final_dockerignore": "",
        "final_report": {},
    }

    compiled = build_multi_service_graph()
    logger.info("Starting multi-service pipeline for %s", project_id)
    try:
        await compiled.ainvoke(initial)
    except Exception as exc:
        logger.exception("Multi-service pipeline crashed for %s", project_id)
        from core.ai.orchestrator import _is_transient_gemini_crash
        from core.ai.pipeline_errors import TransientGeminiError

        summary = _pipeline_crash_error_summary(exc)
        if _is_transient_gemini_crash(exc):
            raise TransientGeminiError(summary) from exc
        async with async_session_factory() as db:
            proj = await db.get(Project, project_id)
            if proj:
                proj.status = "failed"
                proj.error_summary = summary
                await db.commit()
