from __future__ import annotations

import json
import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

from core.analysis.fingerprint import FileTree

logger = logging.getLogger(__name__)

_WORKSPACE_CONFIG_FILES = frozenset({
    "pnpm-workspace.yaml",
    "lerna.json",
    "nx.json",
    "turbo.json",
})

_SERVICE_DIR_NAMES = frozenset({
    "services",
    "apps",
    "packages",
    "microservices",
    "backend",
    "frontend",
    "server",
    "client",
    "web",
    "api",
})

_DEP_FILE_NAMES = frozenset({
    "package.json",
    "requirements.txt",
    "go.mod",
    "Cargo.toml",
    "pyproject.toml",
    "composer.json",
    "Gemfile",
    "pom.xml",
    "build.gradle",
    "mix.exs",
})

_COMPOSE_FILENAMES = frozenset({
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
})

_FRAMEWORK_TYPE_MAP: dict[str, str] = {
    "nextjs": "web",
    "nuxt": "web",
    "react": "web",
    "vue": "web",
    "angular": "web",
    "svelte": "web",
    "gatsby": "web",
    "express": "api",
    "fastapi": "api",
    "flask": "api",
    "django": "web",
    "rails": "web",
    "spring": "api",
    "gin": "api",
    "fiber": "api",
    "echo": "api",
    "actix": "api",
    "laravel": "web",
    "nestjs": "api",
    "koa": "api",
    "hapi": "api",
    "fastify": "api",
    "phoenix": "web",
}

_DEP_LANG_MAP: dict[str, str] = {
    "package.json": "javascript",
    "requirements.txt": "python",
    "pyproject.toml": "python",
    "go.mod": "go",
    "Cargo.toml": "rust",
    "composer.json": "php",
    "Gemfile": "ruby",
    "pom.xml": "java",
    "build.gradle": "java",
    "mix.exs": "elixir",
}

_DATABASE_IMAGES = frozenset({
    "postgres", "postgresql", "mysql", "mariadb", "mongo", "mongodb",
    "redis", "memcached", "elasticsearch", "cassandra", "couchdb",
    "dynamodb", "neo4j", "influxdb", "clickhouse", "minio",
})


@dataclass
class Service:
    name: str
    root_path: str
    language: str | None
    framework: str | None
    type: str
    port: int | None
    depends_on: list[str] = field(default_factory=list)
    has_own_deps: bool = False


@dataclass
class MonorepoResult:
    is_monorepo: bool
    services: list[Service]
    shared_root_deps: bool
    workspace_manager: str | None
    recommended_build_order: list[str]
    detection_method: str


class MonorepoDetector:
    """Detects whether a project is a monorepo containing multiple services."""

    def detect(self, root_path: str, file_tree: FileTree) -> MonorepoResult:
        root = Path(root_path)
        services: list[Service] = []
        detection_method = "single"
        workspace_manager: str | None = None
        shared_root_deps = False

        file_paths = [n.path for n in file_tree.nodes]
        file_set = frozenset(file_paths)

        ws_manager, ws_services = self._detect_workspace_config(root, file_set)
        if ws_services:
            workspace_manager = ws_manager
            services.extend(ws_services)
            detection_method = "workspace_config"
            shared_root_deps = True
            logger.info(
                "Workspace config detected (%s): %d services",
                ws_manager, len(ws_services),
            )

        if not services:
            compose_services = self._detect_compose(root, file_tree)
            if compose_services:
                services.extend(compose_services)
                detection_method = "compose"
                logger.info(
                    "Existing compose file: %d services", len(compose_services),
                )

        if not services:
            dir_services = self._detect_directory_convention(root, file_set)
            if dir_services:
                services.extend(dir_services)
                detection_method = "directory_convention"
                logger.info(
                    "Directory convention: %d services", len(dir_services),
                )

        if not services:
            dep_services, has_root = self._detect_multiple_deps(root, file_set)
            if dep_services:
                services.extend(dep_services)
                detection_method = "multi_deps"
                shared_root_deps = has_root
                logger.info(
                    "Multiple dep files: %d services", len(dep_services),
                )

        if not services and shared_root_deps is False:
            has_root_dep = any(
                name in file_set for name in _DEP_FILE_NAMES
            )
            shared_root_deps = has_root_dep

        for svc in services:
            if svc.type == "unknown" and svc.framework:
                svc.type = _FRAMEWORK_TYPE_MAP.get(
                    svc.framework.lower(), "unknown",
                )

        is_monorepo = len(services) > 1
        build_order = self._topological_sort(services) if services else []

        return MonorepoResult(
            is_monorepo=is_monorepo,
            services=services,
            shared_root_deps=shared_root_deps,
            workspace_manager=workspace_manager,
            recommended_build_order=build_order,
            detection_method=detection_method if is_monorepo else "single",
        )

    # ------------------------------------------------------------------
    # Workspace config detection
    # ------------------------------------------------------------------

    def _detect_workspace_config(
        self,
        root: Path,
        file_set: frozenset[str],
    ) -> tuple[str | None, list[Service]]:
        pkg_json = root / "package.json"
        if pkg_json.is_file():
            try:
                data = json.loads(pkg_json.read_text(errors="replace"))
                workspaces = data.get("workspaces")
                if workspaces:
                    globs = workspaces if isinstance(workspaces, list) else workspaces.get("packages", [])
                    manager = self._infer_js_package_manager(root, file_set)
                    services = self._resolve_workspace_globs(root, globs, file_set)
                    if services:
                        return manager, services
            except (json.JSONDecodeError, OSError):
                logger.debug("Failed to parse root package.json", exc_info=True)

        if "pnpm-workspace.yaml" in file_set:
            try:
                content = (root / "pnpm-workspace.yaml").read_text(errors="replace")
                data = yaml.safe_load(content)
                if isinstance(data, dict):
                    globs = data.get("packages", [])
                    services = self._resolve_workspace_globs(root, globs, file_set)
                    if services:
                        return "pnpm", services
            except (yaml.YAMLError, OSError):
                logger.debug("Failed to parse pnpm-workspace.yaml", exc_info=True)

        for config_name in ("lerna.json", "nx.json", "turbo.json"):
            if config_name in file_set:
                manager_name = config_name.split(".")[0]
                try:
                    data = json.loads(
                        (root / config_name).read_text(errors="replace"),
                    )
                    pkg_globs: list[str] = []
                    if config_name == "lerna.json":
                        pkg_globs = data.get("packages", ["packages/*"])
                    elif config_name == "nx.json":
                        pkg_globs = ["apps/*", "libs/*", "packages/*"]
                    elif config_name == "turbo.json":
                        pkg_globs = ["apps/*", "packages/*"]

                    services = self._resolve_workspace_globs(root, pkg_globs, file_set)
                    if services:
                        return manager_name, services
                except (json.JSONDecodeError, OSError):
                    logger.debug("Failed to parse %s", config_name, exc_info=True)

        return None, []

    @staticmethod
    def _infer_js_package_manager(
        root: Path,
        file_set: frozenset[str],
    ) -> str:
        if "pnpm-lock.yaml" in file_set or "pnpm-workspace.yaml" in file_set:
            return "pnpm"
        if "yarn.lock" in file_set:
            return "yarn"
        return "npm"

    def _resolve_workspace_globs(
        self,
        root: Path,
        globs: list[str],
        file_set: frozenset[str],
    ) -> list[Service]:
        found_dirs: set[str] = set()
        for pattern in globs:
            clean = pattern.rstrip("/").rstrip("/*").rstrip("/**")
            parent = root / clean
            if parent.is_dir():
                for child in sorted(parent.iterdir()):
                    if child.is_dir() and not child.name.startswith("."):
                        found_dirs.add(str(child.relative_to(root)))
            else:
                for child in sorted(root.glob(pattern)):
                    if child.is_dir() and not child.name.startswith("."):
                        found_dirs.add(str(child.relative_to(root)))

        services: list[Service] = []
        for rel_dir in sorted(found_dirs):
            svc = self._build_service_from_dir(root, rel_dir, file_set)
            if svc:
                services.append(svc)
        return services

    # ------------------------------------------------------------------
    # Compose file detection
    # ------------------------------------------------------------------

    def _detect_compose(
        self,
        root: Path,
        file_tree: FileTree,
    ) -> list[Service]:
        if file_tree.existing_compose:
            return self._parse_existing_compose(file_tree.existing_compose)
        return []

    def _parse_existing_compose(self, compose_content: str) -> list[Service]:
        try:
            data = yaml.safe_load(compose_content)
        except yaml.YAMLError:
            logger.warning("Failed to parse existing docker-compose", exc_info=True)
            return []

        if not isinstance(data, dict):
            return []

        raw_services = data.get("services", {})
        if not isinstance(raw_services, dict):
            return []

        services: list[Service] = []
        for name, config in raw_services.items():
            if not isinstance(config, dict):
                continue

            port = self._extract_compose_port(config)
            svc_type = self._infer_compose_service_type(name, config)
            build_ctx = config.get("build")
            if isinstance(build_ctx, dict):
                root_path = build_ctx.get("context", ".")
            elif isinstance(build_ctx, str):
                root_path = build_ctx
            else:
                root_path = "."

            deps: list[str] = []
            raw_deps = config.get("depends_on")
            if isinstance(raw_deps, list):
                deps = raw_deps
            elif isinstance(raw_deps, dict):
                deps = list(raw_deps.keys())

            services.append(Service(
                name=name,
                root_path=root_path,
                language=None,
                framework=None,
                type=svc_type,
                port=port,
                depends_on=deps,
                has_own_deps=build_ctx is not None,
            ))

        return services

    @staticmethod
    def _extract_compose_port(config: dict) -> int | None:
        ports = config.get("ports", [])
        if not ports:
            return None
        first = ports[0]
        if isinstance(first, str):
            parts = first.split(":")
            try:
                return int(parts[0])
            except (ValueError, IndexError):
                return None
        if isinstance(first, dict):
            return first.get("published")
        return None

    @staticmethod
    def _infer_compose_service_type(name: str, config: dict) -> str:
        image = str(config.get("image", "")).lower()
        name_lower = name.lower()

        for db_keyword in _DATABASE_IMAGES:
            if db_keyword in image or db_keyword in name_lower:
                return "database"

        if any(k in name_lower for k in ("frontend", "web", "client", "ui")):
            return "web"
        if any(k in name_lower for k in ("api", "backend", "server", "gateway")):
            return "api"
        if any(k in name_lower for k in ("worker", "queue", "consumer", "cron")):
            return "worker"

        return "unknown"

    # ------------------------------------------------------------------
    # Directory convention detection
    # ------------------------------------------------------------------

    def _detect_directory_convention(
        self,
        root: Path,
        file_set: frozenset[str],
    ) -> list[Service]:
        services: list[Service] = []
        for dir_name in sorted(_SERVICE_DIR_NAMES):
            candidate = root / dir_name
            if not candidate.is_dir():
                continue

            has_inner_dep = any(
                str(PurePosixPath(dir_name) / dep) in file_set
                for dep in _DEP_FILE_NAMES
            )
            has_inner_dockerfile = str(PurePosixPath(dir_name) / "Dockerfile") in file_set

            if has_inner_dep or has_inner_dockerfile:
                svc = self._build_service_from_dir(root, dir_name, file_set)
                if svc:
                    services.append(svc)
                continue

            for child in sorted(candidate.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                rel = str(child.relative_to(root))
                svc = self._build_service_from_dir(root, rel, file_set)
                if svc:
                    services.append(svc)

        return services

    # ------------------------------------------------------------------
    # Multiple dependency file detection
    # ------------------------------------------------------------------

    def _detect_multiple_deps(
        self,
        root: Path,
        file_set: frozenset[str],
    ) -> tuple[list[Service], bool]:
        dep_dirs: dict[str, list[str]] = defaultdict(list)
        has_root_dep = False

        for fpath in file_set:
            p = PurePosixPath(fpath)
            if p.name in _DEP_FILE_NAMES:
                parent = str(p.parent) if str(p.parent) != "." else "."
                dep_dirs[parent].append(p.name)
                if parent == ".":
                    has_root_dep = True

        non_root = {d: files for d, files in dep_dirs.items() if d != "."}
        if len(non_root) < 2:
            return [], has_root_dep

        services: list[Service] = []
        for rel_dir in sorted(non_root):
            svc = self._build_service_from_dir(root, rel_dir, file_set)
            if svc:
                services.append(svc)
        return services, has_root_dep

    # ------------------------------------------------------------------
    # Service builder helper
    # ------------------------------------------------------------------

    def _build_service_from_dir(
        self,
        root: Path,
        rel_dir: str,
        file_set: frozenset[str],
    ) -> Service | None:
        abs_dir = root / rel_dir
        if not abs_dir.is_dir():
            return None

        dep_file_name: str | None = None
        for dep in _DEP_FILE_NAMES:
            check = str(PurePosixPath(rel_dir) / dep)
            if check in file_set:
                dep_file_name = dep
                break

        has_dockerfile = str(PurePosixPath(rel_dir) / "Dockerfile") in file_set

        if not dep_file_name and not has_dockerfile:
            return None

        language = _DEP_LANG_MAP.get(dep_file_name, None) if dep_file_name else None
        framework = self._sniff_framework(root, rel_dir, dep_file_name)
        svc_type = _FRAMEWORK_TYPE_MAP.get(
            (framework or "").lower(), "unknown",
        )
        port = self._sniff_port(framework)
        name = PurePosixPath(rel_dir).name

        return Service(
            name=name,
            root_path=rel_dir,
            language=language,
            framework=framework,
            type=svc_type,
            port=port,
            depends_on=[],
            has_own_deps=dep_file_name is not None,
        )

    @staticmethod
    def _sniff_framework(
        root: Path,
        rel_dir: str,
        dep_file_name: str | None,
    ) -> str | None:
        if dep_file_name != "package.json":
            return None
        pkg_path = root / rel_dir / "package.json"
        try:
            data = json.loads(pkg_path.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            return None

        all_deps = {
            **data.get("dependencies", {}),
            **data.get("devDependencies", {}),
        }
        if "next" in all_deps:
            return "nextjs"
        if "nuxt" in all_deps:
            return "nuxt"
        if "@angular/core" in all_deps:
            return "angular"
        if "vue" in all_deps:
            return "vue"
        if "react" in all_deps and "next" not in all_deps:
            return "react"
        if "svelte" in all_deps:
            return "svelte"
        if "express" in all_deps:
            return "express"
        if "@nestjs/core" in all_deps:
            return "nestjs"
        if "fastify" in all_deps:
            return "fastify"
        if "koa" in all_deps:
            return "koa"
        if "hapi" in all_deps or "@hapi/hapi" in all_deps:
            return "hapi"
        return None

    @staticmethod
    def _sniff_port(framework: str | None) -> int | None:
        port_map: dict[str, int] = {
            "nextjs": 3000,
            "nuxt": 3000,
            "react": 3000,
            "vue": 8080,
            "angular": 4200,
            "svelte": 5173,
            "express": 3000,
            "nestjs": 3000,
            "fastify": 3000,
            "koa": 3000,
            "hapi": 3000,
            "fastapi": 8000,
            "flask": 5000,
            "django": 8000,
            "rails": 3000,
            "spring": 8080,
            "gin": 8080,
            "fiber": 3000,
            "echo": 8080,
            "actix": 8080,
            "laravel": 8000,
            "phoenix": 4000,
        }
        if not framework:
            return None
        return port_map.get(framework.lower())

    # ------------------------------------------------------------------
    # Topological sort for build order
    # ------------------------------------------------------------------

    @staticmethod
    def _topological_sort(services: list[Service]) -> list[str]:
        name_set = {s.name for s in services}
        graph: dict[str, list[str]] = {s.name: [] for s in services}
        in_degree: dict[str, int] = {s.name: 0 for s in services}

        for svc in services:
            for dep in svc.depends_on:
                if dep in name_set:
                    graph[dep].append(svc.name)
                    in_degree[svc.name] += 1

        queue: deque[str] = deque(
            name for name, deg in in_degree.items() if deg == 0
        )
        order: list[str] = []

        while queue:
            node = queue.popleft()
            order.append(node)
            for neighbor in graph[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(order) != len(services):
            logger.warning(
                "Cycle detected in service dependencies; returning partial order",
            )
            missing = name_set - set(order)
            order.extend(sorted(missing))

        return order
