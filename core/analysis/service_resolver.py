from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from core.analysis.fingerprint import ProjectFingerprint
from core.analysis.monorepo import MonorepoDetector, MonorepoResult
from core.manifest.primary import pick_primary_service
from core.manifest.schema import ManifestService
from core.project_config import ProjectConfig, load_project_config

logger = logging.getLogger(__name__)


@dataclass
class ResolvedService:
    name: str
    root_path: str
    type: str
    port: int | None = None
    depends_on: list[str] | None = None
    language: str | None = None
    framework: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "root_path": self.root_path,
            "type": self.type,
            "port": self.port,
            "depends_on": list(self.depends_on or []),
            "language": self.language,
            "framework": self.framework,
        }


def resolve_services(
    repo_root: str,
    root_fingerprint: ProjectFingerprint | dict,
    *,
    file_tree=None,
    monorepo: MonorepoResult | None = None,
) -> tuple[list[ResolvedService], list[str]]:
    """Resolve deployable services: config hints > monorepo detect > single app."""
    root = Path(repo_root).resolve()
    config = load_project_config(str(root))
    build_order: list[str] = []

    if config.has_service_hints():
        services = [
            ResolvedService(
                name=h.name,
                root_path=h.path.strip().lstrip("./"),
                type="api",
                port=h.port,
                language=h.language,
                framework=h.framework,
            )
            for h in config.services
        ]
        build_order = [s.name for s in services]
        return services, build_order

    fp_dict = (
        root_fingerprint
        if isinstance(root_fingerprint, dict)
        else root_fingerprint.to_dict()
    )

    if monorepo is None and file_tree is not None:
        monorepo = MonorepoDetector().detect(str(root), file_tree)

    if monorepo is None:
        from core.analysis.engine import AnalysisEngine

        # Minimal tree for detection when caller did not pass monorepo
        engine = AnalysisEngine()
        tree = engine._build_file_tree(root, [])  # noqa: SLF001
        monorepo = MonorepoDetector().detect(str(root), tree)

    if monorepo.is_monorepo and monorepo.services:
        services = [
            ResolvedService(
                name=svc.name,
                root_path=svc.root_path,
                type=svc.type,
                port=svc.port,
                depends_on=list(svc.depends_on),
                language=svc.language,
                framework=svc.framework,
            )
            for svc in monorepo.services
            if svc.type != "database"
        ]
        db_services = [
            ResolvedService(
                name=svc.name,
                root_path=svc.root_path,
                type="database",
                port=svc.port,
                depends_on=list(svc.depends_on),
            )
            for svc in monorepo.services
            if svc.type == "database"
        ]
        services.extend(db_services)
        build_order = list(monorepo.recommended_build_order) or [s.name for s in services]
        return services[:], build_order

    if fp_dict.get("is_monorepo") and fp_dict.get("services"):
        services = []
        for raw in fp_dict["services"]:
            if not isinstance(raw, dict):
                continue
            services.append(
                ResolvedService(
                    name=str(raw.get("name") or "app"),
                    root_path=str(raw.get("root_path") or "."),
                    type=str(raw.get("type") or "api"),
                    port=raw.get("port"),
                    depends_on=raw.get("depends_on") if isinstance(raw.get("depends_on"), list) else [],
                )
            )
        if services:
            return services, [s.name for s in services]

    return [ResolvedService(name="app", root_path=".", type="api")], ["app"]


def pick_primary(resolved: list[ResolvedService]) -> str | None:
    manifest_svcs = [
        ManifestService(
            name=s.name,
            root_path=s.root_path,
            type=s.type,  # type: ignore[arg-type]
            port=s.port,
        )
        for s in resolved
        if s.type != "database"
    ]
    return pick_primary_service(manifest_svcs)
