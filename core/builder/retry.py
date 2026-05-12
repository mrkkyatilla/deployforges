from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field

import redis.asyncio as redis

from api.config import settings
from core.ai.dockerfile_generator import DockerfileGenerator
from core.ai.token_manager import TokenBudget
from core.builder.sandbox import BuildResult, KanikoBuildSandbox, DockerBuildSandbox
from core.builder.validator import CloudRunValidator, DeploymentResult
from core.error.classifier import BuildErrorClassifier
from core.error.parser import extract_error_lines, get_last_n_lines

logger = logging.getLogger(__name__)

ATTEMPT_STRATEGY: dict[int, str] = {
    1: "initial_build",
    2: "ai_fix_with_error_context",
    3: "ai_fix_with_full_log",
    4: "template_fallback_with_ai_customization",
    5: "conservative_build_maximum_compatibility",
}

MAX_ATTEMPTS = 5

_CONSERVATIVE_TEMPLATE = """\
FROM python:3.12-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || \\
    pip install --no-cache-dir -e . 2>/dev/null || \\
    echo "No Python dependencies to install"

EXPOSE 8000

CMD ["python", "-m", "http.server", "8000"]
"""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AttemptRecord:
    attempt: int
    strategy: str
    dockerfile: str
    dockerignore: str
    build_result: BuildResult | None = None
    deploy_result: DeploymentResult | None = None
    errors: list[dict] = field(default_factory=list)
    duration_ms: int = 0


@dataclass
class FinalBuildResult:
    success: bool
    final_dockerfile: str = ""
    final_dockerignore: str = ""
    attempts: list[AttemptRecord] = field(default_factory=list)
    deploy_url: str | None = None
    total_tokens: int = 0
    failure_analysis: str = ""


# ---------------------------------------------------------------------------
# BuildRetryManager
# ---------------------------------------------------------------------------

class BuildRetryManager:
    """Orchestrates the build → validate → fix retry loop."""

    def __init__(
        self,
        sandbox: KanikoBuildSandbox | DockerBuildSandbox | None = None,
        cloud_validator: CloudRunValidator | None = None,
        error_classifier: BuildErrorClassifier | None = None,
        dockerfile_generator: DockerfileGenerator | None = None,
        redis_url: str | None = None,
    ):
        self.sandbox = sandbox or KanikoBuildSandbox()
        self.cloud_validator = cloud_validator or CloudRunValidator()
        self.classifier = error_classifier or BuildErrorClassifier()
        self.generator = dockerfile_generator or DockerfileGenerator()
        self._redis_url = redis_url or settings.redis_url

    async def run(
        self,
        project_id: str,
        project_path: str,
        port: int,
        language: str,
        dockerfile: str,
        dockerignore: str,
        fingerprint: dict,
        token_budget: TokenBudget | int = 50_000,
    ) -> FinalBuildResult:
        if isinstance(token_budget, int):
            token_budget = TokenBudget(total=token_budget)

        attempts: list[AttemptRecord] = []
        current_dockerfile = dockerfile
        current_dockerignore = dockerignore

        for attempt_num in range(1, MAX_ATTEMPTS + 1):
            strategy = ATTEMPT_STRATEGY.get(attempt_num, "ai_fix_with_full_log")
            build_id = f"{project_id[:8]}-a{attempt_num}-{uuid.uuid4().hex[:6]}"

            logger.info(
                "Build attempt %d/%d for project %s — strategy: %s",
                attempt_num, MAX_ATTEMPTS, project_id, strategy,
            )
            await self._emit_event(project_id, "attempt_start", {
                "attempt": attempt_num,
                "strategy": strategy,
            })

            record = AttemptRecord(
                attempt=attempt_num,
                strategy=strategy,
                dockerfile=current_dockerfile,
                dockerignore=current_dockerignore,
            )

            # ----- Build -----
            start = time.monotonic()
            build_result = await self.sandbox.build(
                project_path=project_path,
                dockerfile_content=current_dockerfile,
                build_id=build_id,
                dockerignore_content=current_dockerignore,
            )
            record.build_result = build_result
            record.duration_ms = int((time.monotonic() - start) * 1000)

            await self._emit_event(project_id, "build_complete", {
                "attempt": attempt_num,
                "success": build_result.success,
                "duration_ms": build_result.duration_ms,
            })

            if not build_result.success:
                classified = self.classifier.classify(
                    build_result.logs or build_result.error_output,
                    language,
                )
                record.errors = [
                    {
                        "type": e.error_type,
                        "name": e.name,
                        "severity": e.severity,
                        "auto_fixable": e.auto_fixable,
                        "match_text": e.match_text[:200],
                    }
                    for e in classified
                ]

                attempts.append(record)

                if attempt_num >= MAX_ATTEMPTS:
                    break

                current_dockerfile, current_dockerignore = await self._apply_fix(
                    attempt_num=attempt_num,
                    current_dockerfile=current_dockerfile,
                    current_dockerignore=current_dockerignore,
                    build_result=build_result,
                    fingerprint=fingerprint,
                    language=language,
                    token_budget=token_budget,
                )
                continue

            # ----- Deploy & Validate -----
            await self._emit_event(project_id, "deploy_start", {"attempt": attempt_num})

            deploy_result = await self.cloud_validator.validate(
                image_ref=build_result.image_ref,  # type: ignore[arg-type]
                port=port,
            )
            record.deploy_result = deploy_result
            attempts.append(record)

            await self._emit_event(project_id, "deploy_complete", {
                "attempt": attempt_num,
                "success": deploy_result.success,
            })

            if deploy_result.success:
                return FinalBuildResult(
                    success=True,
                    final_dockerfile=current_dockerfile,
                    final_dockerignore=current_dockerignore,
                    attempts=attempts,
                    deploy_url=deploy_result.service_url,
                    total_tokens=token_budget.spent,
                )

            # Deploy failed — treat as build error for the next iteration
            if attempt_num < MAX_ATTEMPTS:
                error_ctx = deploy_result.error or "Deployment health/smoke checks failed"
                build_result_for_fix = BuildResult(
                    success=False,
                    logs=error_ctx,
                    error_output=error_ctx,
                )
                current_dockerfile, current_dockerignore = await self._apply_fix(
                    attempt_num=attempt_num,
                    current_dockerfile=current_dockerfile,
                    current_dockerignore=current_dockerignore,
                    build_result=build_result_for_fix,
                    fingerprint=fingerprint,
                    language=language,
                    token_budget=token_budget,
                )

        # All attempts exhausted
        failure_analysis = self._build_failure_analysis(attempts)
        await self._emit_event(project_id, "pipeline_failed", {
            "total_attempts": len(attempts),
            "analysis": failure_analysis,
        })

        return FinalBuildResult(
            success=False,
            final_dockerfile=current_dockerfile,
            final_dockerignore=current_dockerignore,
            attempts=attempts,
            total_tokens=token_budget.spent,
            failure_analysis=failure_analysis,
        )

    # -- fix strategies -------------------------------------------------------

    async def _apply_fix(
        self,
        attempt_num: int,
        current_dockerfile: str,
        current_dockerignore: str,
        build_result: BuildResult,
        fingerprint: dict,
        language: str,
        token_budget: TokenBudget,
    ) -> tuple[str, str]:
        next_strategy = ATTEMPT_STRATEGY.get(attempt_num + 1, "ai_fix_with_full_log")

        try:
            if next_strategy == "ai_fix_with_error_context":
                return await self._fix_with_error_context(
                    current_dockerfile, current_dockerignore,
                    build_result, fingerprint, attempt_num + 1, token_budget,
                )

            if next_strategy == "ai_fix_with_full_log":
                return await self._fix_with_full_log(
                    current_dockerfile, current_dockerignore,
                    build_result, fingerprint, attempt_num + 1, token_budget,
                )

            if next_strategy == "template_fallback_with_ai_customization":
                return await self._fix_template_fallback(
                    current_dockerignore, fingerprint, language,
                    attempt_num + 1, token_budget,
                )

            if next_strategy == "conservative_build_maximum_compatibility":
                return self._conservative_fallback(language)

        except Exception:
            logger.exception("Fix strategy %s failed, falling back to current Dockerfile", next_strategy)

        return current_dockerfile, current_dockerignore

    async def _fix_with_error_context(
        self,
        dockerfile: str,
        dockerignore: str,
        build_result: BuildResult,
        fingerprint: dict,
        attempt: int,
        budget: TokenBudget,
    ) -> tuple[str, str]:
        error_ctx = extract_error_lines(build_result.logs or build_result.error_output)
        error_ctx = error_ctx[:2000]

        result = await self.generator.fix(
            dockerfile=dockerfile,
            error_context=error_ctx,
            fingerprint=fingerprint,
            attempt_number=attempt,
            token_budget=budget,
        )
        return result.dockerfile or dockerfile, result.dockerignore or dockerignore

    async def _fix_with_full_log(
        self,
        dockerfile: str,
        dockerignore: str,
        build_result: BuildResult,
        fingerprint: dict,
        attempt: int,
        budget: TokenBudget,
    ) -> tuple[str, str]:
        full_log = build_result.logs or build_result.error_output
        error_ctx = get_last_n_lines(full_log, 80)

        result = await self.generator.fix(
            dockerfile=dockerfile,
            error_context=error_ctx,
            fingerprint=fingerprint,
            attempt_number=attempt,
            token_budget=budget,
        )
        return result.dockerfile or dockerfile, result.dockerignore or dockerignore

    async def _fix_template_fallback(
        self,
        dockerignore: str,
        fingerprint: dict,
        language: str,
        attempt: int,
        budget: TokenBudget,
    ) -> tuple[str, str]:
        result = await self.generator.generate(
            fingerprint=fingerprint,
            project_path=fingerprint.get("project_path", "."),
            token_budget=budget,
        )
        return result.dockerfile or _CONSERVATIVE_TEMPLATE, result.dockerignore or dockerignore

    @staticmethod
    def _conservative_fallback(language: str) -> tuple[str, str]:
        templates: dict[str, str] = {
            "python": (
                "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\n"
                "RUN pip install --no-cache-dir -r requirements.txt 2>/dev/null || true\n"
                "EXPOSE 8000\nCMD [\"python\", \"manage.py\", \"runserver\", \"0.0.0.0:8000\"]"
            ),
            "node": (
                "FROM node:20-slim\nWORKDIR /app\nCOPY package*.json ./\n"
                "RUN npm ci --omit=dev 2>/dev/null || npm install\n"
                "COPY . .\nEXPOSE 3000\nCMD [\"node\", \"index.js\"]"
            ),
            "go": (
                "FROM golang:1.22-alpine AS build\nWORKDIR /app\n"
                "COPY go.* ./\nRUN go mod download\nCOPY . .\n"
                "RUN CGO_ENABLED=0 go build -o /server .\n"
                "FROM alpine:3.19\nCOPY --from=build /server /server\n"
                "EXPOSE 8080\nCMD [\"/server\"]"
            ),
        }
        dockerfile = templates.get(language, _CONSERVATIVE_TEMPLATE)
        ignore = ".git\nnode_modules\n__pycache__\n*.pyc\n.env\n"
        return dockerfile, ignore

    # -- events ---------------------------------------------------------------

    async def _emit_event(
        self, project_id: str, event_type: str, data: dict
    ) -> None:
        payload = json.dumps({"project_id": project_id, "event": event_type, **data})
        try:
            r = redis.from_url(self._redis_url, decode_responses=True)
            async with r:
                await r.publish(f"build:{project_id}", payload)
        except Exception:
            logger.debug("Redis publish failed for event %s", event_type, exc_info=True)

    # -- analysis helper ------------------------------------------------------

    @staticmethod
    def _build_failure_analysis(attempts: list[AttemptRecord]) -> str:
        lines = [f"Build failed after {len(attempts)} attempt(s).\n"]

        for rec in attempts:
            status = "BUILD_OK" if rec.build_result and rec.build_result.success else "BUILD_FAIL"
            lines.append(f"Attempt {rec.attempt} [{rec.strategy}] — {status}")

            if rec.errors:
                top_errors = rec.errors[:3]
                for err in top_errors:
                    lines.append(f"  - {err.get('name', '?')}: {err.get('match_text', '')[:120]}")

            if rec.deploy_result and not rec.deploy_result.success:
                lines.append(f"  Deploy error: {rec.deploy_result.error or 'checks failed'}")

        return "\n".join(lines)
