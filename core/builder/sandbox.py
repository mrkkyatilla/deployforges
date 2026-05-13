from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from api.config import settings

logger = logging.getLogger(__name__)

_DOCKERFILE_DIRECTIVES = re.compile(
    r"^\s*(FROM|RUN|COPY|ADD|CMD|ENTRYPOINT|WORKDIR|EXPOSE|ENV|ARG|LABEL|"
    r"VOLUME|USER|HEALTHCHECK|SHELL|STOPSIGNAL|ONBUILD)\s",
    re.IGNORECASE,
)
_COPY_SRC_RE = re.compile(
    r"^\s*COPY\s+(?:--[a-z-]+=\S+\s+)*(.+)\s+\S+\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_FROM_IMAGE_RE = re.compile(
    r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)",
    re.IGNORECASE | re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BuildResult:
    success: bool
    image_ref: str | None = None
    image_digest: str | None = None
    logs: str = ""
    error_output: str = ""
    duration_ms: int = 0


@dataclass
class CheckResult:
    name: str
    is_error: bool
    details: str = ""


@dataclass
class ValidationResult:
    can_build: bool
    errors: list[CheckResult] = field(default_factory=list)
    skip_reason: str | None = None


# ---------------------------------------------------------------------------
# PreBuildValidator
# ---------------------------------------------------------------------------

class PreBuildValidator:
    """Runs lightweight pre-build sanity checks before invoking a full build."""

    async def validate(
        self, project_path: str, dockerfile: str
    ) -> ValidationResult:
        checks = await asyncio.gather(
            self._check_dockerfile_syntax(dockerfile),
            self._check_referenced_files_exist(project_path, dockerfile),
            self._check_base_image_exists(dockerfile),
            self._check_context_size(project_path),
            return_exceptions=True,
        )

        results: list[CheckResult] = []
        for item in checks:
            if isinstance(item, BaseException):
                results.append(
                    CheckResult(name="internal", is_error=True, details=str(item))
                )
            elif isinstance(item, list):
                results.extend(item)
            else:
                results.append(item)

        has_error = any(r.is_error for r in results)
        return ValidationResult(can_build=not has_error, errors=results)

    # -- individual checks ---------------------------------------------------

    async def _check_dockerfile_syntax(self, dockerfile: str) -> list[CheckResult]:
        results: list[CheckResult] = []
        lines = [ln for ln in dockerfile.splitlines() if ln.strip() and not ln.strip().startswith("#")]

        if not lines:
            results.append(CheckResult("dockerfile_syntax", True, "Dockerfile is empty"))
            return results

        has_from = any(re.match(r"^\s*FROM\s", ln, re.IGNORECASE) for ln in lines)
        if not has_from:
            results.append(CheckResult("dockerfile_syntax", True, "Missing FROM instruction"))

        has_cmd_or_entrypoint = any(
            re.match(r"^\s*(CMD|ENTRYPOINT)\s", ln, re.IGNORECASE) for ln in lines
        )
        if not has_cmd_or_entrypoint:
            results.append(
                CheckResult("dockerfile_syntax", False, "No CMD or ENTRYPOINT — container may not start")
            )

        for i, ln in enumerate(lines, 1):
            stripped = ln.strip()
            if stripped and not stripped.startswith("#") and not _DOCKERFILE_DIRECTIVES.match(stripped):
                if not stripped.startswith("AS ") and "=" not in stripped:
                    results.append(
                        CheckResult("dockerfile_syntax", False, f"Unrecognised line {i}: {stripped[:80]}")
                    )

        if not results:
            results.append(CheckResult("dockerfile_syntax", False, "Syntax OK"))
        return results

    async def _check_referenced_files_exist(
        self, project_path: str, dockerfile: str
    ) -> list[CheckResult]:
        results: list[CheckResult] = []
        root = Path(project_path)

        for m in _COPY_SRC_RE.finditer(dockerfile):
            sources_part = m.group(1).strip()
            sources = sources_part.split()
            for src in sources:
                if src.startswith("--"):
                    continue
                if src == "." or "*" in src or "$" in src:
                    continue
                src_path = root / src
                if not src_path.exists():
                    results.append(
                        CheckResult("referenced_files", True, f"COPY source not found: {src}")
                    )

        if not results:
            results.append(CheckResult("referenced_files", False, "All referenced files exist"))
        return results

    async def _check_base_image_exists(self, dockerfile: str) -> list[CheckResult]:
        results: list[CheckResult] = []

        for m in _FROM_IMAGE_RE.finditer(dockerfile):
            image = m.group(1)
            if image.lower() == "scratch" or image.startswith("$"):
                continue

            parts = image.split("/")
            if len(parts) == 1:
                library, repo_tag = "library", parts[0]
            elif len(parts) == 2:
                library, repo_tag = parts
            else:
                results.append(CheckResult("base_image", False, f"Skipped registry check for {image}"))
                continue

            repo = repo_tag.split(":")[0]
            tag = repo_tag.split(":")[1] if ":" in repo_tag else "latest"
            url = f"https://registry.hub.docker.com/v2/{library}/{repo}/manifests/{tag}"

            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.head(
                        url,
                        headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"},
                    )
                    if resp.status_code >= 400:
                        if resp.status_code in (401, 403, 429):
                            results.append(
                                CheckResult(
                                    "base_image",
                                    False,
                                    f"Registry returned HTTP {resp.status_code} for {image} "
                                    "(auth/rate limit); cannot verify remotely — continuing.",
                                )
                            )
                        else:
                            results.append(
                                CheckResult(
                                    "base_image",
                                    True,
                                    f"Base image may not exist: {image} (HTTP {resp.status_code})",
                                )
                            )
            except httpx.HTTPError:
                results.append(
                    CheckResult("base_image", False, f"Could not verify base image: {image} (network error)")
                )

        if not results:
            results.append(CheckResult("base_image", False, "Base images OK"))
        return results

    async def _check_context_size(self, project_path: str) -> CheckResult:
        total = 0
        max_bytes = 500 * 1024 * 1024  # 500 MB
        root = Path(project_path)

        for dirpath, _dirnames, filenames in os.walk(root):
            for f in filenames:
                try:
                    total += (Path(dirpath) / f).stat().st_size
                except OSError:
                    pass
            if total > max_bytes:
                return CheckResult(
                    "context_size",
                    True,
                    f"Build context too large: {total / (1024 * 1024):.0f} MB (limit 500 MB)",
                )

        size_mb = total / (1024 * 1024)
        return CheckResult("context_size", False, f"Context size: {size_mb:.1f} MB")


# ---------------------------------------------------------------------------
# KanikoBuildSandbox — GCP Cloud Run Jobs + Kaniko
# ---------------------------------------------------------------------------

class KanikoBuildSandbox:
    """Builds Docker images in an isolated Cloud Run Job using kaniko."""

    def __init__(
        self,
        gcp_project: str | None = None,
        gcp_region: str | None = None,
    ):
        self.project = gcp_project or settings.gcp_project_id
        self.region = gcp_region or settings.gcp_region

    async def build(
        self,
        project_path: str,
        dockerfile_content: str,
        build_id: str,
        dockerignore_content: str | None = None,
    ) -> BuildResult:
        start = time.monotonic()
        gcs_context = f"gs://{self.project}-build-contexts/{build_id}/context.tar.gz"
        image_ref = f"gcr.io/{self.project}/builds/{build_id}"
        job_name = f"build-{build_id[:48]}"

        root = Path(project_path)
        (root / "Dockerfile").write_text(dockerfile_content)
        if dockerignore_content:
            (root / ".dockerignore").write_text(dockerignore_content)

        try:
            # Upload build context
            tar_path = await self._create_and_upload_context(root, gcs_context)

            # Create & execute Cloud Run Job
            await self._create_job(job_name, image_ref, gcs_context)
            logs, exit_code = await self._execute_job(job_name)

            duration = int((time.monotonic() - start) * 1000)

            if exit_code != 0:
                return BuildResult(
                    success=False,
                    logs=logs,
                    error_output=logs[-5000:] if len(logs) > 5000 else logs,
                    duration_ms=duration,
                )

            digest = await self._get_image_digest(image_ref)
            return BuildResult(
                success=True,
                image_ref=image_ref,
                image_digest=digest,
                logs=logs,
                duration_ms=duration,
            )
        except asyncio.TimeoutError:
            duration = int((time.monotonic() - start) * 1000)
            return BuildResult(
                success=False,
                logs="",
                error_output=f"Build timed out after {settings.build_timeout_seconds}s",
                duration_ms=duration,
            )
        except Exception as exc:
            duration = int((time.monotonic() - start) * 1000)
            logger.exception("Build %s failed with unexpected error", build_id)
            return BuildResult(
                success=False,
                logs="",
                error_output=str(exc),
                duration_ms=duration,
            )
        finally:
            await self._cleanup(gcs_context, job_name)

    # -- internal helpers ----------------------------------------------------

    async def _create_and_upload_context(
        self, root: Path, gcs_dest: str
    ) -> str:
        with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
            tar_path = tmp.name

        try:
            proc = await asyncio.create_subprocess_exec(
                "tar", "czf", tar_path, "-C", str(root), ".",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"tar failed: {stderr.decode()}")

            proc = await asyncio.create_subprocess_exec(
                "gcloud", "storage", "cp", tar_path, gcs_dest,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(f"GCS upload failed: {stderr.decode()}")
        finally:
            Path(tar_path).unlink(missing_ok=True)

        return gcs_dest

    async def _create_job(
        self, job_name: str, destination: str, gcs_context: str
    ) -> None:
        kaniko_image = "gcr.io/kaniko-project/executor:latest"

        cmd = [
            "gcloud", "run", "jobs", "create", job_name,
            "--image", kaniko_image,
            "--region", self.region,
            "--project", self.project,
            "--cpu", settings.build_cpu_limit,
            "--memory", settings.build_memory_limit,
            "--task-timeout", f"{settings.build_timeout_seconds}s",
            "--max-retries", "0",
            "--args", ",".join([
                f"--context={gcs_context}",
                f"--destination={destination}",
                "--cache=true",
                f"--cache-repo=gcr.io/{self.project}/cache",
            ]),
            "--quiet",
            "--format", "json",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to create Cloud Run Job: {stderr.decode()}")

    async def _execute_job(self, job_name: str) -> tuple[str, int]:
        exec_cmd = [
            "gcloud", "run", "jobs", "execute", job_name,
            "--region", self.region,
            "--project", self.project,
            "--wait",
            "--format", "json",
        ]

        proc = await asyncio.create_subprocess_exec(
            *exec_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=settings.build_timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise

        logs = await self._fetch_execution_logs(job_name)
        return logs, proc.returncode or 0

    async def _fetch_execution_logs(self, job_name: str) -> str:
        cmd = [
            "gcloud", "logging", "read",
            f'resource.type="cloud_run_job" AND resource.labels.job_name="{job_name}"',
            "--project", self.project,
            "--limit", "500",
            "--format", "value(textPayload)",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        return stdout.decode(errors="replace")

    async def _get_image_digest(self, image_ref: str) -> str | None:
        cmd = [
            "gcloud", "container", "images", "describe", image_ref,
            "--format", "value(image_summary.digest)",
            "--project", self.project,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        digest = stdout.decode().strip()
        return digest or None

    async def _cleanup(self, gcs_context: str, job_name: str) -> None:
        for cmd in [
            ["gcloud", "storage", "rm", gcs_context, "--quiet"],
            [
                "gcloud", "run", "jobs", "delete", job_name,
                "--region", self.region,
                "--project", self.project,
                "--quiet",
            ],
        ]:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await asyncio.wait_for(proc.communicate(), timeout=30)
            except Exception:
                logger.warning("Cleanup command failed: %s", " ".join(cmd), exc_info=True)


# ---------------------------------------------------------------------------
# DockerBuildSandbox — local Docker daemon (dev / self-hosted fallback)
# ---------------------------------------------------------------------------

class DockerBuildSandbox:
    """Builds Docker images via the local Docker daemon."""

    async def build(
        self,
        project_path: str,
        dockerfile_content: str,
        build_id: str,
        dockerignore_content: str | None = None,
    ) -> BuildResult:
        start = time.monotonic()
        tag = f"build-{build_id}"
        root = Path(project_path)

        (root / "Dockerfile").write_text(dockerfile_content)
        if dockerignore_content:
            (root / ".dockerignore").write_text(dockerignore_content)

        cmd = [
            "docker", "build",
            "-t", tag,
            "-f", "Dockerfile",
            "--memory", settings.build_memory_limit.lower().replace("gi", "g"),
            "--cpu-period", "100000",
            "--cpu-quota", str(int(float(settings.build_cpu_limit) * 100_000)),
            ".",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(root),
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=settings.build_timeout_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                duration = int((time.monotonic() - start) * 1000)
                return BuildResult(
                    success=False,
                    logs="",
                    error_output=f"Build timed out after {settings.build_timeout_seconds}s",
                    duration_ms=duration,
                )

            logs = stdout.decode(errors="replace") + stderr.decode(errors="replace")
            duration = int((time.monotonic() - start) * 1000)

            if proc.returncode != 0:
                return BuildResult(
                    success=False,
                    logs=logs,
                    error_output=logs[-5000:] if len(logs) > 5000 else logs,
                    duration_ms=duration,
                )

            digest = await self._get_image_digest(tag)
            return BuildResult(
                success=True,
                image_ref=tag,
                image_digest=digest,
                logs=logs,
                duration_ms=duration,
            )

        except Exception as exc:
            duration = int((time.monotonic() - start) * 1000)
            logger.exception("Local docker build %s failed", build_id)
            return BuildResult(
                success=False,
                logs="",
                error_output=str(exc),
                duration_ms=duration,
            )

    async def _get_image_digest(self, tag: str) -> str | None:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", "--format", "{{.Id}}", tag,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        digest = stdout.decode().strip()
        return digest or None
