"""Deployment manifest v1 — portable deploy contract for hosting integrators."""

from core.manifest.builder import build_deployment_manifest
from core.manifest.primary import pick_primary_service
from core.manifest.schema import (
    DeploymentManifest,
    ManifestArtifacts,
    ManifestProject,
    ManifestService,
    ManifestValidation,
    ServiceArtifact,
    ServiceBuildSpec,
    ServiceEnvVar,
    ServiceHealthSpec,
    ServiceRunSpec,
)

__all__ = [
    "DeploymentManifest",
    "ManifestArtifacts",
    "ManifestProject",
    "ManifestService",
    "ManifestValidation",
    "ServiceArtifact",
    "ServiceBuildSpec",
    "ServiceEnvVar",
    "ServiceHealthSpec",
    "ServiceRunSpec",
    "build_deployment_manifest",
    "pick_primary_service",
]
