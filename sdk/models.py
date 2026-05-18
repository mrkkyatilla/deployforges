from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from sdk.exceptions import DeployForgeError


@dataclass
class DeployResult:
    """Final deployment artefacts produced by the analysis pipeline."""

    dockerfile: str
    dockerignore: str | None = None
    docker_compose: str | None = None
    warnings: list[str] = field(default_factory=list)
    build_stats: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None


@dataclass
class ManifestDeployResult:
    """v2 API result — DeploymentManifest v1."""

    status: str
    deployment_manifest: dict[str, Any]
    usage: dict[str, Any] | None = None
    error_summary: str | None = None


@dataclass
class BuildResult:
    """Result for an individual build attempt."""

    attempt: int
    status: str
    error_type: str | None = None
    duration_seconds: int | None = None


@dataclass
class Project:
    """Represents a DeployForge project returned by the API."""

    id: str
    status: str
    source_type: str
    source_url: str | None
    created_at: str

    _client: Any = field(repr=False, default=None)

    def wait(self, timeout: int = 600, poll_interval: int = 3) -> Project:
        """Poll until the project reaches a terminal status or *timeout* elapses.

        Returns the refreshed ``Project`` instance.

        Raises ``DeployForgeError`` if the project fails or the timeout is
        exceeded.
        """
        if self._client is None:
            raise DeployForgeError("Project is not bound to a client")

        deadline = time.monotonic() + timeout
        current = self
        terminal = {"completed", "success", "partial", "failed", "cancelled"}

        while current.status not in terminal:
            if time.monotonic() >= deadline:
                raise DeployForgeError(
                    f"Timed out after {timeout}s waiting for project {self.id}"
                )
            time.sleep(poll_interval)
            current = self._client.projects.get(self.id)
            current._client = self._client

        if current.status == "failed":
            raise DeployForgeError(f"Project {self.id} failed")

        return current

    def result(self) -> DeployResult:
        """Fetch the final deployment result for this project."""
        if self._client is None:
            raise DeployForgeError("Project is not bound to a client")
        return self._client.projects.result(self.id)

    def events(self):
        """Stream SSE events for this project.

        Yields ``dict`` payloads for each server-sent event.
        """
        if self._client is None:
            raise DeployForgeError("Project is not bound to a client")
        yield from self._client.projects.events(self.id)
