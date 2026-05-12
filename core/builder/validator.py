from __future__ import annotations

import asyncio
import json
import logging
import uuid
import time
from dataclasses import dataclass, field

import httpx

from api.config import settings

logger = logging.getLogger(__name__)

_HEALTH_ENDPOINTS = ["/", "/health", "/healthz", "/api/health", "/ping"]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class InspectionResult:
    size_mb: float = 0.0
    layer_count: int = 0
    exposed_ports: list[str] = field(default_factory=list)
    user: str = "root"
    has_healthcheck: bool = False
    raw: dict = field(default_factory=dict)


@dataclass
class HealthCheckResult:
    healthy: bool
    endpoint: str | None = None
    status_code: int | None = None
    latency_ms: int = 0
    details: str = ""


@dataclass
class SmokeTest:
    name: str
    description: str


@dataclass
class SmokeTestResult:
    test: SmokeTest
    passed: bool
    details: str = ""


@dataclass
class DeploymentResult:
    success: bool
    service_url: str | None = None
    health: HealthCheckResult | None = None
    smoke_tests: list[SmokeTestResult] = field(default_factory=list)
    logs: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# ImageInspector
# ---------------------------------------------------------------------------

class ImageInspector:
    """Extracts metadata from a built Docker image via ``docker inspect``."""

    async def inspect(self, image_ref: str) -> InspectionResult:
        proc = await asyncio.create_subprocess_exec(
            "docker", "inspect", image_ref,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)

        if proc.returncode != 0:
            logger.error("docker inspect failed for %s: %s", image_ref, stderr.decode())
            return InspectionResult()

        try:
            data = json.loads(stdout.decode())
        except json.JSONDecodeError:
            logger.error("Failed to parse docker inspect output for %s", image_ref)
            return InspectionResult()

        if not data:
            return InspectionResult()

        info = data[0]
        config = info.get("Config", {})

        size_bytes = info.get("Size", 0)
        layers = info.get("RootFS", {}).get("Layers", [])
        ports = list((config.get("ExposedPorts") or {}).keys())
        user = config.get("User", "") or "root"
        has_hc = bool(config.get("Healthcheck"))

        return InspectionResult(
            size_mb=round(size_bytes / (1024 * 1024), 2),
            layer_count=len(layers),
            exposed_ports=ports,
            user=user,
            has_healthcheck=has_hc,
            raw=info,
        )


# ---------------------------------------------------------------------------
# CloudRunValidator
# ---------------------------------------------------------------------------

class CloudRunValidator:
    """Deploys an image to Cloud Run, runs health / smoke checks, then tears down."""

    def __init__(
        self,
        gcp_project: str | None = None,
        gcp_region: str | None = None,
    ):
        self.project = gcp_project or settings.gcp_project_id
        self.region = gcp_region or settings.gcp_region

    async def validate(self, image_ref: str, port: int) -> DeploymentResult:
        service_name = f"validate-{uuid.uuid4().hex[:8]}"
        service_url: str | None = None

        try:
            service_url = await self._deploy(service_name, image_ref, port)
            if not service_url:
                return DeploymentResult(
                    success=False,
                    error="Failed to deploy — no service URL returned",
                )

            await self._wait_for_ready(service_url)

            health = await self._health_check(service_url)
            smoke_results = await self._smoke_tests(service_url)
            all_passed = health.healthy and all(s.passed for s in smoke_results)

            return DeploymentResult(
                success=all_passed,
                service_url=service_url,
                health=health,
                smoke_tests=smoke_results,
            )

        except asyncio.TimeoutError:
            return DeploymentResult(
                success=False,
                service_url=service_url,
                error="Deployment timed out waiting for service readiness",
            )
        except Exception as exc:
            logger.exception("Cloud Run validation failed for %s", image_ref)
            return DeploymentResult(
                success=False,
                service_url=service_url,
                error=str(exc),
            )
        finally:
            await self._cleanup(service_name)

    # -- internal helpers ----------------------------------------------------

    async def _deploy(self, name: str, image_ref: str, port: int) -> str | None:
        cmd = [
            "gcloud", "run", "deploy", name,
            "--image", image_ref,
            "--port", str(port),
            "--region", self.region,
            "--project", self.project,
            "--allow-unauthenticated",
            "--max-instances", str(settings.cloud_run_max_instances),
            "--memory", "512Mi",
            "--timeout", str(settings.cloud_run_timeout),
            "--no-cpu-throttling",
            "--quiet",
            "--format", "json",
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=settings.cloud_run_timeout,
        )

        if proc.returncode != 0:
            logger.error("Cloud Run deploy failed: %s", stderr.decode())
            return None

        try:
            data = json.loads(stdout.decode())
            return data.get("status", {}).get("url") or data.get("status", {}).get("address", {}).get("url")
        except (json.JSONDecodeError, KeyError):
            logger.error("Could not parse Cloud Run deploy output")
            return None

    async def _wait_for_ready(
        self,
        url: str,
        poll_interval: float = 3.0,
        max_wait: float = 60.0,
    ) -> None:
        deadline = time.monotonic() + max_wait
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
            while time.monotonic() < deadline:
                try:
                    resp = await client.get(url)
                    if resp.status_code < 500:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(poll_interval)

        raise asyncio.TimeoutError(f"Service at {url} not ready within {max_wait}s")

    async def _health_check(self, base_url: str) -> HealthCheckResult:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            for endpoint in _HEALTH_ENDPOINTS:
                url = base_url.rstrip("/") + endpoint
                start = time.monotonic()
                try:
                    resp = await client.get(url)
                    latency = int((time.monotonic() - start) * 1000)
                    if resp.status_code < 500:
                        return HealthCheckResult(
                            healthy=True,
                            endpoint=endpoint,
                            status_code=resp.status_code,
                            latency_ms=latency,
                        )
                except httpx.HTTPError as exc:
                    latency = int((time.monotonic() - start) * 1000)
                    logger.debug("Health check %s failed: %s", endpoint, exc)

        return HealthCheckResult(healthy=False, details="All health endpoints returned 5xx or failed")

    async def _smoke_tests(self, base_url: str) -> list[SmokeTestResult]:
        results: list[SmokeTestResult] = []

        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            try:
                resp = await client.get(base_url)
            except httpx.HTTPError as exc:
                return [
                    SmokeTestResult(
                        test=SmokeTest("root_accessible", "Root URL returns < 500"),
                        passed=False,
                        details=str(exc),
                    ),
                ]

            results.append(SmokeTestResult(
                test=SmokeTest("root_accessible", "Root URL returns < 500"),
                passed=resp.status_code < 500,
                details=f"Status {resp.status_code}",
            ))

            body = resp.content
            results.append(SmokeTestResult(
                test=SmokeTest("non_empty_response", "Response body is not empty"),
                passed=len(body) > 0,
                details=f"{len(body)} bytes",
            ))

            results.append(SmokeTestResult(
                test=SmokeTest("has_content_type", "Response includes Content-Type header"),
                passed="content-type" in resp.headers,
                details=resp.headers.get("content-type", "missing"),
            ))

        return results

    async def _cleanup(self, service_name: str) -> None:
        cmd = [
            "gcloud", "run", "services", "delete", service_name,
            "--region", self.region,
            "--project", self.project,
            "--quiet",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except Exception:
            logger.warning("Failed to clean up Cloud Run service %s", service_name, exc_info=True)
