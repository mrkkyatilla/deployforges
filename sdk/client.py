"""DeployForge Python SDK client.

Usage::

    from deployforge_sdk import DeployForge

    client = DeployForge(api_key="df_live_xxx")
    project = client.projects.create(
        source_type="git",
        source_url="https://github.com/user/repo",
    )
    project = project.wait()
    print(project.result().dockerfile)
"""
from __future__ import annotations

from typing import Any

import httpx

from sdk.exceptions import (
    AuthenticationError,
    DeployForgeError,
    InsufficientCreditsError,
    ProjectNotFoundError,
    RateLimitError,
)
from sdk.resources import BillingResource, ProjectsResource, ProjectsV2Resource

_DEFAULT_ENDPOINT = "https://api.deployforge.dev"
_DEFAULT_TIMEOUT = 30.0


class DeployForge:
    """DeployForge Python SDK client."""

    def __init__(
        self,
        api_key: str,
        endpoint: str = _DEFAULT_ENDPOINT,
        timeout: float = _DEFAULT_TIMEOUT,
        *,
        api_version: str = "v1",
    ) -> None:
        self._api_key = api_key
        self._endpoint = endpoint.rstrip("/")
        self._api_version = api_version
        self._http = httpx.Client(
            base_url=self._endpoint,
            timeout=timeout,
        )
        self._projects = ProjectsResource(self)
        self._projects_v2 = ProjectsV2Resource(self)
        self._billing = BillingResource(self)

    @property
    def projects(self) -> ProjectsResource | ProjectsV2Resource:
        if self._api_version == "v2":
            return self._projects_v2
        return self._projects

    @property
    def projects_v2(self) -> ProjectsV2Resource:
        return self._projects_v2

    @property
    def billing(self) -> BillingResource:
        return self._billing

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Execute an API request and return the decoded JSON body.

        Raises typed exceptions for well-known HTTP error codes.
        """
        try:
            response = self._http.request(
                method,
                path,
                json=json,
                params=params,
                headers=self._auth_headers(),
            )
        except httpx.HTTPError as exc:
            raise DeployForgeError(f"HTTP request failed: {exc}") from exc

        if response.status_code == 401:
            raise AuthenticationError("Invalid or missing API key")
        if response.status_code == 402:
            raise InsufficientCreditsError("Insufficient credits")
        if response.status_code == 404:
            raise ProjectNotFoundError(f"Resource not found: {path}")
        if response.status_code == 429:
            raise RateLimitError("Rate limit exceeded")
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except Exception:
                detail = response.text
            raise DeployForgeError(
                f"API error {response.status_code}: {detail}"
            )

        return response.json()

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    def __enter__(self) -> DeployForge:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
