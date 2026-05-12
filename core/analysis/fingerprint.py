from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field


@dataclass
class FileNode:
    path: str
    size_bytes: int
    extension: str
    is_config: bool
    is_source: bool


@dataclass
class FileTree:
    root_path: str
    total_files: int
    total_size_bytes: int
    nodes: list[FileNode]
    existing_dockerfile: str | None = None
    existing_dockerignore: str | None = None
    existing_compose: str | None = None


@dataclass
class LanguageDetection:
    primary: str
    version: str | None
    secondary: list[str]
    scores: dict[str, float]


@dataclass
class FrameworkDetection:
    name: str | None
    version: str | None
    is_static: bool
    default_port: int | None
    build_command: str | None
    start_command: str | None


@dataclass
class Dependency:
    name: str
    version_constraint: str | None
    is_native: bool


@dataclass
class DependencyInfo:
    manager: str
    lock_file_exists: bool
    runtime_deps: list[Dependency]
    dev_deps: list[Dependency]
    system_packages_needed: dict[str, list[str]]
    language_version: str | None


@dataclass
class EntrypointInfo:
    file: str | None
    command: str | None
    detection_method: str  # "config", "convention", "ast"


@dataclass
class PortInfo:
    value: int
    detection_method: str  # "config", "code", "framework_default"
    confidence: str  # "high", "medium", "low"


@dataclass
class ServiceInfo:
    name: str
    root_path: str
    type: str
    port: int | None
    depends_on: list[str] = field(default_factory=list)


@dataclass
class ProjectFingerprint:
    file_tree: FileTree
    language: LanguageDetection
    framework: FrameworkDetection
    dependencies: DependencyInfo
    entrypoint: EntrypointInfo
    port: PortInfo
    environment: dict = field(default_factory=dict)
    security: dict = field(default_factory=dict)
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)
    is_monorepo: bool = False
    services: list[ServiceInfo] = field(default_factory=list)
    monorepo_detection_method: str | None = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)
