from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING, Any

import httpx

from sdk.models import DeployResult, ManifestDeployResult, Project
from sdk.sse import parse_sse_stream

if TYPE_CHECKING:
    from sdk.client import DeployForge


class ProjectsResource:
    """Operations on the ``/api/v1/projects`` endpoint."""

    def __init__(self, client: DeployForge) -> None:
        self._client = client

    def create(self, source_type: str, source_url: str, **kwargs: Any) -> Project:
        """Create and queue a new project.

        *kwargs* are forwarded as additional body fields (e.g.
        ``branch``, ``config_overrides``).
        """
        body: dict[str, Any] = {
            "source_type": source_type,
            "source_url": source_url,
            **kwargs,
        }
        data = self._client._request("POST", "/api/v1/projects", json=body)
        return self._to_project(data)

    def get(self, project_id: str) -> Project:
        """Get the current status of a project."""
        data = self._client._request("GET", f"/api/v1/projects/{project_id}")
        return self._to_project(data)

    def result(self, project_id: str) -> DeployResult:
        """Get the final result (Dockerfile, etc.) for a completed project."""
        data = self._client._request("GET", f"/api/v1/projects/{project_id}/result")
        return DeployResult(
            dockerfile=data["dockerfile"],
            dockerignore=data.get("dockerignore"),
            docker_compose=data.get("docker_compose"),
            warnings=data.get("warnings", []),
            build_stats=data.get("build_stats"),
            usage=data.get("usage"),
        )

    def list(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """List projects, optionally filtered by *status*."""
        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status
        return self._client._request("GET", "/api/v1/projects", params=params)

    def events(self, project_id: str) -> Generator[dict[str, Any], None, None]:
        """Stream server-sent events for a project.

        Yields parsed event dicts as they arrive.
        """
        with self._client._http.stream(
            "GET",
            f"/api/v1/projects/{project_id}/events",
            headers=self._client._auth_headers(),
        ) as response:
            response.raise_for_status()
            yield from parse_sse_stream(response)

    def _to_project(self, data: dict[str, Any]) -> Project:
        return Project(
            id=data["id"],
            status=data["status"],
            source_type=data["source_type"],
            source_url=data.get("source_url"),
            created_at=data["created_at"],
            _client=self._client,
        )


class ProjectsV2Resource:
    """Operations on ``/api/v2/projects`` (DeploymentManifest v1)."""

    def __init__(self, client: DeployForge) -> None:
        self._client = client

    def create(
        self,
        source_type: str,
        source_url: str,
        *,
        branch: str = "main",
        commit: str | None = None,
        **kwargs: Any,
    ) -> Project:
        st = "git" if source_type in ("git", "git_url") else source_type
        body: dict[str, Any] = {
            "source": {
                "type": st,
                "url": source_url,
                "branch": branch,
            },
            **kwargs,
        }
        if commit:
            body["source"]["commit"] = commit
        data = self._client._request("POST", "/api/v2/projects", json=body)
        return Project(
            id=data["id"],
            status=data["status"],
            source_type=source_type,
            source_url=source_url,
            created_at=data["created_at"],
            _client=self._client,
        )

    def get(self, project_id: str) -> dict[str, Any]:
        return self._client._request("GET", f"/api/v2/projects/{project_id}")

    def result(self, project_id: str) -> ManifestDeployResult:
        data = self._client._request("GET", f"/api/v2/projects/{project_id}/result")
        return ManifestDeployResult(
            status=data["status"],
            deployment_manifest=data.get("deployment_manifest") or {},
            usage=data.get("usage"),
            error_summary=data.get("error_summary"),
        )


class BillingResource:
    """Operations on the ``/api/v1/billing`` endpoint."""

    def __init__(self, client: DeployForge) -> None:
        self._client = client

    def credits(self) -> dict[str, Any]:
        """Get the current credit balance."""
        return self._client._request("GET", "/api/v1/billing/credits")

    def usage(self) -> dict[str, Any]:
        """Get a usage report for the current billing period."""
        return self._client._request("GET", "/api/v1/billing/usage")
