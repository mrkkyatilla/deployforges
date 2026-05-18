"""T0: DeploymentManifest v1 schema."""

from __future__ import annotations

from core.manifest.primary import pick_primary_service
from core.manifest.schema import DeploymentManifest, ManifestService


def test_manifest_roundtrip() -> None:
    m = DeploymentManifest(
        services=[
            ManifestService(name="api", root_path="backend", type="api", port=8000),
            ManifestService(name="web", root_path="frontend", type="web", port=3000),
        ],
    )
    data = m.to_dict()
    m2 = DeploymentManifest.from_dict(data)
    assert m2.deployment_manifest_version == "1"
    assert len(m2.services) == 2


def test_pick_primary_prefers_api_with_port() -> None:
    services = [
        ManifestService(name="web", root_path="frontend", type="web", port=3000),
        ManifestService(name="api", root_path="backend", type="api", port=8000),
    ]
    assert pick_primary_service(services) == "api"
