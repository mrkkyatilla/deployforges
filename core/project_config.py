from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_CONFIG_FILENAMES = (".deployforge.yml", ".deployforge.yaml")


@dataclass
class ServiceHint:
    """A user-declared service within a monorepo or multi-service project."""

    name: str
    path: str
    port: int | None = None
    language: str | None = None
    framework: str | None = None


@dataclass
class ProjectConfig:
    """Parsed representation of a ``.deployforge.yml`` project configuration.

    Provides optional hints that guide the analysis and generation pipeline.
    """

    services: list[ServiceHint] = field(default_factory=list)
    env_hints: list[str] = field(default_factory=list)
    build_args: dict[str, str] = field(default_factory=dict)
    max_attempts: int | None = None
    ignore_paths: list[str] = field(default_factory=list)
    base_image_preferences: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_file(cls, project_path: str) -> ProjectConfig | None:
        """Read ``.deployforge.yml`` from *project_path*.

        Returns ``None`` if no config file exists.
        """
        root = Path(project_path)
        for name in _CONFIG_FILENAMES:
            config_file = root / name
            if config_file.is_file():
                try:
                    text = config_file.read_text(encoding="utf-8")
                    data = yaml.safe_load(text)
                    if not isinstance(data, dict):
                        logger.warning(
                            "%s: expected a YAML mapping at the top level", config_file
                        )
                        return cls()
                    return cls.from_dict(data)
                except yaml.YAMLError:
                    logger.warning("Failed to parse %s", config_file, exc_info=True)
                    return cls()
                except OSError:
                    logger.warning("Failed to read %s", config_file, exc_info=True)
                    return cls()
        return None

    @classmethod
    def from_dict(cls, data: dict) -> ProjectConfig:
        """Construct a ``ProjectConfig`` from a raw parsed YAML dict."""
        services = _parse_services(data.get("services"))
        env_hints = _parse_string_list(data.get("env_hints"))
        build_args = _parse_string_dict(data.get("build_args"))
        max_attempts = _parse_optional_int(data.get("max_attempts"))
        ignore_paths = _parse_string_list(data.get("ignore_paths"))
        base_image_preferences = _parse_string_dict(data.get("base_image_preferences"))

        return cls(
            services=services,
            env_hints=env_hints,
            build_args=build_args,
            max_attempts=max_attempts,
            ignore_paths=ignore_paths,
            base_image_preferences=base_image_preferences,
        )

    def has_service_hints(self) -> bool:
        return len(self.services) > 0


def load_project_config(project_path: str) -> ProjectConfig:
    """Load the project configuration from *project_path*.

    Attempts to read ``.deployforge.yml`` or ``.deployforge.yaml``.  Returns
    a validated ``ProjectConfig`` on success, or an empty default instance when
    no config file is present or when parsing fails.

    Service paths are validated against the filesystem; invalid entries are
    dropped with a warning.
    """
    config = ProjectConfig.from_file(project_path)
    if config is None:
        return ProjectConfig()

    _validate_config(config, project_path)
    return config


def _validate_config(config: ProjectConfig, project_path: str) -> None:
    """Validate a parsed config in-place, stripping invalid entries."""
    root = Path(project_path)

    valid_services: list[ServiceHint] = []
    for svc in config.services:
        svc_dir = root / svc.path
        if not svc_dir.is_dir():
            logger.warning(
                "Service '%s' path does not exist: %s — skipping", svc.name, svc_dir
            )
            continue
        if svc.port is not None and not (1 <= svc.port <= 65535):
            logger.warning(
                "Service '%s' has invalid port %s — clearing", svc.name, svc.port
            )
            svc.port = None
        valid_services.append(svc)
    config.services = valid_services

    if config.max_attempts is not None and config.max_attempts < 1:
        logger.warning("max_attempts must be >= 1, got %s — clearing", config.max_attempts)
        config.max_attempts = None


# ------------------------------------------------------------------
# Parsing helpers
# ------------------------------------------------------------------


def _parse_services(raw: object) -> list[ServiceHint]:
    if not isinstance(raw, list):
        return []
    services: list[ServiceHint] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        path = item.get("path")
        if not isinstance(name, str) or not isinstance(path, str):
            logger.warning("Service entry missing 'name' or 'path': %s — skipping", item)
            continue
        services.append(
            ServiceHint(
                name=name,
                path=path,
                port=_parse_optional_int(item.get("port")),
                language=item.get("language") if isinstance(item.get("language"), str) else None,
                framework=item.get("framework") if isinstance(item.get("framework"), str) else None,
            )
        )
    return services


def _parse_string_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(v) for v in raw if isinstance(v, str)]


def _parse_string_dict(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _parse_optional_int(raw: object) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
