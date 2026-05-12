from __future__ import annotations

import json
from collections.abc import Generator
from typing import Any

import httpx


class DeployForgeClient:
    def __init__(self, api_key: str, endpoint: str) -> None:
        self.endpoint = endpoint.rstrip("/")
        self._client = httpx.Client(
            base_url=self.endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "deployforge-cli/0.3.0",
            },
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    def check_health(self) -> bool:
        try:
            resp = self._client.get("/api/v1/health")
            resp.raise_for_status()
            return True
        except httpx.HTTPError:
            return False

    def create_project(
        self,
        source_type: str,
        source_url: str,
        branch: str = "main",
        commit: str | None = None,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_type": source_type,
            "source_url": source_url,
            "branch": branch,
        }
        if commit:
            payload["commit"] = commit
        if options:
            payload.update(options)

        resp = self._client.post("/api/v1/projects", json=payload)
        resp.raise_for_status()
        return resp.json()

    def get_project(self, project_id: str) -> dict[str, Any]:
        resp = self._client.get(f"/api/v1/projects/{project_id}")
        resp.raise_for_status()
        return resp.json()

    def get_result(self, project_id: str) -> dict[str, Any]:
        resp = self._client.get(f"/api/v1/projects/{project_id}/result")
        resp.raise_for_status()
        return resp.json()

    def stream_events(self, project_id: str) -> Generator[dict[str, Any], None, None]:
        url = f"/api/v1/projects/{project_id}/events"
        with self._client.stream("GET", url, headers={"Accept": "text/event-stream"}) as resp:
            resp.raise_for_status()
            event_type: str | None = None
            data_lines: list[str] = []

            for raw_line in resp.iter_lines():
                line = raw_line.strip()

                if not line:
                    if data_lines:
                        raw_data = "\n".join(data_lines)
                        try:
                            parsed = json.loads(raw_data)
                        except json.JSONDecodeError:
                            parsed = {"message": raw_data}
                        yield {"event": event_type or "message", "data": parsed}
                        event_type = None
                        data_lines = []
                    continue

                if line.startswith("event:"):
                    event_type = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].strip())

    def get_usage(self) -> dict[str, Any]:
        resp = self._client.get("/api/v1/billing/usage")
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> DeployForgeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
