"""T1: v2 API contract (schema-level, no live DB)."""

from __future__ import annotations

from uuid import UUID

from api.schemas.manifest import DeploymentManifestResult, ProjectV2ResultResponse
from core.manifest.schema import DeploymentManifest


def test_v2_result_response_shape() -> None:
    m = DeploymentManifest(services=[])
    body = DeploymentManifestResult(**m.model_dump(mode="json"))
    resp = ProjectV2ResultResponse(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        status="success",
        deployment_manifest=body,
    )
    assert resp.deployment_manifest is not None
    assert resp.deployment_manifest.deployment_manifest_version == "1"
