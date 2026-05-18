from __future__ import annotations

from typing import Any

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

_SERVICE_TYPE_MAP = {
    "api": "api",
    "web": "web",
    "worker": "worker",
    "database": "database",
}


def _fw_dict(fp: dict) -> dict[str, Any]:
    fw = fp.get("framework")
    return fw if isinstance(fw, dict) else {}


def _lang_primary(fp: dict) -> str | None:
    lang = fp.get("language")
    if isinstance(lang, dict):
        return lang.get("primary")
    return None


def _port_value(fp: dict) -> int | None:
    port = fp.get("port")
    if isinstance(port, dict):
        v = port.get("value")
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.isdigit():
            return int(v)
    return None


def manifest_service_from_fingerprint(
    name: str,
    root_path: str,
    service_type: str,
    fingerprint: dict,
    *,
    depends_on: list[str] | None = None,
) -> ManifestService:
    fw = _fw_dict(fingerprint)
    env_hints = []
    env_block = fingerprint.get("environment") or {}
    if isinstance(env_block, dict):
        for hint in env_block.get("env_var_hints") or []:
            if isinstance(hint, str):
                env_hints.append(ServiceEnvVar(name=hint, required=False))

    return ManifestService(
        name=name,
        root_path=root_path,
        type=_SERVICE_TYPE_MAP.get(service_type, "unknown"),  # type: ignore[arg-type]
        language=_lang_primary(fingerprint),
        framework=fw.get("name"),
        port=_port_value(fingerprint),
        build=ServiceBuildSpec(command=fw.get("build_command")),
        run=ServiceRunSpec(
            command=fw.get("start_command"),
            port=_port_value(fingerprint),
        ),
        health=ServiceHealthSpec(),
        env=env_hints,
        depends_on=list(depends_on or []),
    )


def build_deployment_manifest(
    *,
    root_fingerprint: dict,
    resolved_services: list[dict],
    service_fingerprints: dict[str, dict],
    service_artifacts: dict[str, dict] | None = None,
    compose_yml: str = "",
    levels_passed: list[str] | None = None,
    cloud_run_service: str | None = None,
    deploy_url: str | None = None,
    warnings: list[str] | None = None,
    source_type: str | None = None,
) -> DeploymentManifest:
    """Assemble manifest from analysis + optional generated artifacts."""
    langs: set[str] = set()
    if _lang_primary(root_fingerprint):
        langs.add(str(_lang_primary(root_fingerprint)))
    for fp in service_fingerprints.values():
        if _lang_primary(fp):
            langs.add(str(_lang_primary(fp)))

    manifest_services: list[ManifestService] = []
    for svc in resolved_services:
        name = str(svc.get("name") or "app")
        root_path = str(svc.get("root_path") or ".")
        svc_type = str(svc.get("type") or "unknown")
        fp = service_fingerprints.get(name) or root_fingerprint
        manifest_services.append(
            manifest_service_from_fingerprint(
                name,
                root_path,
                svc_type,
                fp,
                depends_on=svc.get("depends_on") if isinstance(svc.get("depends_on"), list) else [],
            )
        )

    primary = pick_primary_service(manifest_services)
    artifacts = ManifestArtifacts(compose_yml=compose_yml or "")
    for name, raw in (service_artifacts or {}).items():
        if not isinstance(raw, dict):
            continue
        artifacts.dockerfiles[name] = ServiceArtifact(
            dockerfile=str(raw.get("dockerfile") or ""),
            dockerignore=str(raw.get("dockerignore") or ""),
            generation_method=raw.get("generation_method") or "none",  # type: ignore[arg-type]
            dockerfile_path=raw.get("dockerfile_path"),
        )

    root_warnings = root_fingerprint.get("warnings") or []
    merged_warnings = list(warnings or [])
    if isinstance(root_warnings, list):
        merged_warnings.extend(str(w) for w in root_warnings)

    return DeploymentManifest(
        project=ManifestProject(
            source_type=source_type,
            languages=sorted(langs),
            is_monorepo=bool(root_fingerprint.get("is_monorepo")),
        ),
        services=manifest_services,
        artifacts=artifacts,
        validation=ManifestValidation(
            primary_service=primary,
            levels_passed=list(levels_passed or []),
            cloud_run_service=cloud_run_service,
            deploy_url=deploy_url,
        ),
        warnings=merged_warnings,
    )
