from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

GenerationMethod = Literal["template", "llm", "hybrid", "none"]
ServiceType = Literal["api", "web", "worker", "database", "unknown"]


class ServiceBuildSpec(BaseModel):
    command: str | None = None
    output_path: str | None = None


class ServiceRunSpec(BaseModel):
    command: str | None = None
    port: int | None = None


class ServiceHealthSpec(BaseModel):
    path: str = "/health"
    interval_seconds: int = 30


class ServiceEnvVar(BaseModel):
    name: str
    required: bool = False
    description: str | None = None


class ManifestService(BaseModel):
    name: str
    root_path: str
    type: ServiceType = "unknown"
    language: str | None = None
    framework: str | None = None
    port: int | None = None
    build: ServiceBuildSpec = Field(default_factory=ServiceBuildSpec)
    run: ServiceRunSpec = Field(default_factory=ServiceRunSpec)
    health: ServiceHealthSpec = Field(default_factory=ServiceHealthSpec)
    env: list[ServiceEnvVar] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class ServiceArtifact(BaseModel):
    dockerfile: str = ""
    dockerignore: str = ""
    generation_method: GenerationMethod = "none"
    dockerfile_path: str | None = None


class ManifestArtifacts(BaseModel):
    dockerfiles: dict[str, ServiceArtifact] = Field(default_factory=dict)
    compose_yml: str = ""


class ManifestValidation(BaseModel):
    primary_service: str | None = None
    levels_passed: list[str] = Field(default_factory=list)
    cloud_run_service: str | None = None
    deploy_url: str | None = None


class ManifestProject(BaseModel):
    source_type: str | None = None
    languages: list[str] = Field(default_factory=list)
    is_monorepo: bool = False


class DeploymentManifest(BaseModel):
    deployment_manifest_version: str = "1"
    project: ManifestProject = Field(default_factory=ManifestProject)
    services: list[ManifestService] = Field(default_factory=list)
    artifacts: ManifestArtifacts = Field(default_factory=ManifestArtifacts)
    validation: ManifestValidation = Field(default_factory=ManifestValidation)
    warnings: list[str] = Field(default_factory=list)

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")

    @classmethod
    def from_dict(cls, data: dict | None) -> DeploymentManifest:
        if not data:
            return cls()
        return cls.model_validate(data)
