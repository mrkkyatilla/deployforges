from __future__ import annotations

import logging
from pathlib import Path

from core.analysis.dependency import DependencyAnalyzer
from core.analysis.entrypoint import EntrypointDetector
from core.analysis.fingerprint import (
    FileNode,
    FileTree,
    FrameworkDetection,
    PortInfo,
    ProjectFingerprint,
    ServiceInfo,
)
from core.analysis.monorepo import MonorepoDetector, MonorepoResult, Service
from core.analysis.framework_detector import FrameworkDetector
from core.analysis.language_detector import LanguageDetector
from core.analysis.port_detector import PortDetector
from core.project_config import ProjectConfig, load_project_config

logger = logging.getLogger(__name__)

_SKIP_DIRS: set[str] = {
    "node_modules",
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "dist",
    "build",
    "target",
    ".next",
    ".nuxt",
    "vendor",
    "coverage",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "egg-info",
}

_CONFIG_EXTENSIONS: set[str] = {
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".cfg",
    ".ini",
    ".xml",
    ".env",
    ".lock",
}

_SOURCE_EXTENSIONS: set[str] = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".java",
    ".php",
    ".rb",
    ".cs",
    ".ex",
    ".exs",
    ".mjs",
    ".cjs",
}

_CONFIG_FILENAMES: set[str] = {
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
    ".dockerignore",
    "Makefile",
    "Procfile",
    "Gemfile",
    "Rakefile",
    "Cargo.toml",
    "go.mod",
    "go.sum",
    "requirements.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "package.json",
    "tsconfig.json",
    "pom.xml",
    "build.gradle",
    "mix.exs",
    "composer.json",
    ".gitignore",
    ".env",
    ".env.example",
}


class AnalysisEngine:

    def __init__(self) -> None:
        self._language_detector = LanguageDetector()
        self._framework_detector = FrameworkDetector()
        self._dependency_analyzer = DependencyAnalyzer()
        self._entrypoint_detector = EntrypointDetector()
        self._port_detector = PortDetector()
        self._monorepo_detector = MonorepoDetector()

    async def analyze(self, project_path: str) -> ProjectFingerprint:
        warnings: list[str] = []
        root = Path(project_path).resolve()

        if not root.is_dir():
            raise FileNotFoundError(f"Project path does not exist: {root}")

        # 0. Load optional .deployforge.yml config
        try:
            project_config = load_project_config(str(root))
        except Exception:
            logger.warning("Failed to load .deployforge.yml", exc_info=True)
            project_config = ProjectConfig()

        # Apply ignore_paths to the skip-dirs set for this run
        extra_skip = self._extra_skip_dirs(project_config)

        # 1. Build FileTree
        file_tree = self._build_file_tree(root, warnings, extra_skip_dirs=extra_skip)

        # 2. Monorepo detection (service hints may override)
        try:
            monorepo = self._monorepo_detector.detect(str(root), file_tree)
            if project_config.has_service_hints():
                monorepo = self._apply_service_hints(monorepo, project_config, root)
            if monorepo.is_monorepo:
                logger.info(
                    "Monorepo detected (%s): %d services",
                    monorepo.detection_method, len(monorepo.services),
                )
        except Exception:
            logger.error("Monorepo detection failed", exc_info=True)
            warnings.append("Monorepo detection failed")
            monorepo = None

        # 3. Language detection
        try:
            language = self._language_detector.detect(file_tree)
        except Exception:
            logger.error("Language detection failed", exc_info=True)
            warnings.append("Language detection failed; defaulting to 'unknown'")
            from core.analysis.fingerprint import LanguageDetection

            language = LanguageDetection(
                primary="unknown", version=None, secondary=[], scores={}
            )

        # 4. Framework detection
        try:
            framework = self._framework_detector.detect(
                file_tree, language.primary, str(root)
            )
        except Exception:
            logger.error("Framework detection failed", exc_info=True)
            warnings.append("Framework detection failed")
            framework = FrameworkDetection(
                name=None,
                version=None,
                is_static=False,
                default_port=None,
                build_command=None,
                start_command=None,
            )

        # 5. Dependency analysis
        try:
            dependencies = self._dependency_analyzer.analyze(str(root), language.primary)
        except Exception:
            logger.error("Dependency analysis failed", exc_info=True)
            warnings.append("Dependency analysis failed")
            from core.analysis.fingerprint import DependencyInfo

            dependencies = DependencyInfo(
                manager="unknown",
                lock_file_exists=False,
                runtime_deps=[],
                dev_deps=[],
                system_packages_needed={},
                language_version=None,
            )

        # 6. Entrypoint detection
        try:
            entrypoint = self._entrypoint_detector.detect(
                str(root), language.primary, framework.name
            )
        except Exception:
            logger.error("Entrypoint detection failed", exc_info=True)
            warnings.append("Entrypoint detection failed")
            from core.analysis.fingerprint import EntrypointInfo

            entrypoint = EntrypointInfo(
                file=None, command=None, detection_method="none"
            )

        # 7. Port detection
        try:
            port = self._port_detector.detect(
                str(root), language.primary, framework, entrypoint
            )
        except Exception:
            logger.error("Port detection failed", exc_info=True)
            warnings.append("Port detection failed; defaulting to 8080")
            port = PortInfo(
                value=8080, detection_method="framework_default", confidence="low"
            )

        # 8. Environment info
        environment = self._collect_environment(root)
        if project_config.env_hints:
            environment.setdefault("env_var_hints", []).extend(project_config.env_hints)
            environment["requires_env_vars"] = True

        # 8b. Attach base_image_preferences and build_args from config
        if project_config.base_image_preferences:
            environment["base_image_preferences"] = dict(project_config.base_image_preferences)
        if project_config.build_args:
            environment["build_args"] = dict(project_config.build_args)

        # 9. Security info
        security = self._collect_security(root, file_tree)

        # 10. Calculate confidence
        confidence = self._calculate_confidence(
            language, framework, dependencies, entrypoint, port, warnings
        )

        # 11. Build monorepo service info
        svc_infos: list[ServiceInfo] = []
        is_monorepo = False
        detection_method: str | None = None
        if monorepo and monorepo.is_monorepo:
            is_monorepo = True
            detection_method = monorepo.detection_method
            for svc in monorepo.services:
                svc_infos.append(ServiceInfo(
                    name=svc.name,
                    root_path=svc.root_path,
                    type=svc.type,
                    port=svc.port,
                    depends_on=list(svc.depends_on),
                ))

        return ProjectFingerprint(
            file_tree=file_tree,
            language=language,
            framework=framework,
            dependencies=dependencies,
            entrypoint=entrypoint,
            port=port,
            environment=environment,
            security=security,
            confidence=confidence,
            warnings=warnings,
            is_monorepo=is_monorepo,
            services=svc_infos,
            monorepo_detection_method=detection_method,
        )

    # ------------------------------------------------------------------
    # Monorepo analysis
    # ------------------------------------------------------------------

    async def analyze_monorepo(
        self,
        project_path: str,
    ) -> tuple[MonorepoResult, dict[str, ProjectFingerprint]]:
        """Detect monorepo structure and analyze each service individually.

        Returns the monorepo detection result and a mapping of service name
        to its per-service ``ProjectFingerprint``.
        """
        root = Path(project_path).resolve()
        if not root.is_dir():
            raise FileNotFoundError(f"Project path does not exist: {root}")

        file_tree = self._build_file_tree(root, [])
        monorepo = self._monorepo_detector.detect(str(root), file_tree)

        per_service: dict[str, ProjectFingerprint] = {}
        if monorepo.is_monorepo:
            for svc in monorepo.services:
                svc_path = root / svc.root_path
                if not svc_path.is_dir():
                    logger.warning(
                        "Service directory not found: %s", svc_path,
                    )
                    continue
                try:
                    fp = await self.analyze(str(svc_path))
                    per_service[svc.name] = fp
                except Exception:
                    logger.error(
                        "Analysis failed for service %s", svc.name,
                        exc_info=True,
                    )
        else:
            fp = await self.analyze(project_path)
            per_service["root"] = fp

        return monorepo, per_service

    async def analyze_service(
        self,
        repo_root: str,
        service_root: str,
        *,
        service_name: str | None = None,
    ) -> ProjectFingerprint:
        """Analyze a single service directory within a monorepo repository."""
        repo = Path(repo_root).resolve()
        rel = service_root.strip().lstrip("./")
        svc_path = (repo / rel).resolve()
        if not svc_path.is_dir():
            raise FileNotFoundError(f"Service path does not exist: {svc_path}")

        fp = await self.analyze(str(svc_path))
        env = dict(fp.environment or {})
        env["parent_monorepo"] = True
        env["repo_root"] = str(repo)
        env["service_root_rel"] = rel
        if service_name:
            env["service_name"] = service_name
        fp.environment = env
        return fp

    # ------------------------------------------------------------------
    # FileTree builder
    # ------------------------------------------------------------------

    @staticmethod
    def _build_file_tree(
        root: Path,
        warnings: list[str],
        extra_skip_dirs: set[str] | None = None,
    ) -> FileTree:
        nodes: list[FileNode] = []
        total_size = 0
        existing_dockerfile: str | None = None
        existing_dockerignore: str | None = None
        existing_compose: str | None = None

        skip = _SKIP_DIRS | extra_skip_dirs if extra_skip_dirs else _SKIP_DIRS

        try:
            for fpath in sorted(root.rglob("*")):
                if not fpath.is_file():
                    continue

                relative = fpath.relative_to(root)

                if any(part in skip for part in relative.parts):
                    continue

                try:
                    size = fpath.stat().st_size
                except OSError:
                    size = 0

                ext = fpath.suffix.lower()
                name = fpath.name
                rel_str = str(relative)

                is_config = ext in _CONFIG_EXTENSIONS or name in _CONFIG_FILENAMES
                is_source = ext in _SOURCE_EXTENSIONS

                nodes.append(
                    FileNode(
                        path=rel_str,
                        size_bytes=size,
                        extension=ext,
                        is_config=is_config,
                        is_source=is_source,
                    )
                )
                total_size += size

                if name == "Dockerfile" and existing_dockerfile is None:
                    try:
                        existing_dockerfile = fpath.read_text(errors="replace")
                    except OSError:
                        pass
                elif name == ".dockerignore" and existing_dockerignore is None:
                    try:
                        existing_dockerignore = fpath.read_text(errors="replace")
                    except OSError:
                        pass
                elif name in (
                    "docker-compose.yml",
                    "docker-compose.yaml",
                    "compose.yml",
                    "compose.yaml",
                ) and existing_compose is None:
                    try:
                        existing_compose = fpath.read_text(errors="replace")
                    except OSError:
                        pass
        except OSError:
            logger.error("Failed to walk project directory", exc_info=True)
            warnings.append("File tree scan encountered OS errors")

        return FileTree(
            root_path=str(root),
            total_files=len(nodes),
            total_size_bytes=total_size,
            nodes=nodes,
            existing_dockerfile=existing_dockerfile,
            existing_dockerignore=existing_dockerignore,
            existing_compose=existing_compose,
        )

    # ------------------------------------------------------------------
    # .deployforge.yml helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extra_skip_dirs(config: ProjectConfig) -> set[str]:
        """Derive additional skip directory names from the config's *ignore_paths*."""
        extra: set[str] = set()
        for p in config.ignore_paths:
            cleaned = p.strip().lstrip("./")
            if cleaned:
                extra.add(Path(cleaned).parts[0] if Path(cleaned).parts else cleaned)
        return extra

    @staticmethod
    def _apply_service_hints(
        monorepo: MonorepoResult,
        config: ProjectConfig,
        root: Path,
    ) -> MonorepoResult:
        """Merge user-provided service hints into the detected monorepo result.

        If the detector did not find a monorepo but the config declares multiple
        services, the hints take precedence.
        """
        existing_names = {s.name for s in monorepo.services}
        for hint in config.services:
            if hint.name in existing_names:
                for svc in monorepo.services:
                    if svc.name == hint.name:
                        if hint.port is not None and svc.port is None:
                            svc.port = hint.port
                        if hint.language and not svc.language:
                            svc.language = hint.language
                        if hint.framework and not svc.framework:
                            svc.framework = hint.framework
                        break
            else:
                svc_dir = root / hint.path
                if svc_dir.is_dir():
                    monorepo.services.append(
                        Service(
                            name=hint.name,
                            root_path=hint.path.lstrip("./"),
                            language=hint.language,
                            framework=hint.framework,
                            type="unknown",
                            port=hint.port,
                            has_own_deps=False,
                        )
                    )

        if len(monorepo.services) > 1 and not monorepo.is_monorepo:
            monorepo.is_monorepo = True
            monorepo.detection_method = "deployforge_config"

        return monorepo

    # ------------------------------------------------------------------
    # Environment collection
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_environment(root: Path) -> dict:
        env_info: dict = {
            "requires_env_vars": False,
            "has_env_file": False,
            "env_files_found": [],
            "warnings": [],
        }

        env_files = [".env", ".env.example", ".env.sample", ".env.local", ".env.development"]
        for name in env_files:
            if (root / name).is_file():
                env_info["has_env_file"] = True
                env_info["env_files_found"].append(name)
                if name == ".env":
                    env_info["warnings"].append(
                        ".env file found — ensure secrets are not baked into the image"
                    )

        if env_info["env_files_found"]:
            env_info["requires_env_vars"] = True

        return env_info

    # ------------------------------------------------------------------
    # Security checks
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_security(root: Path, file_tree: FileTree) -> dict:
        security: dict = {
            "secrets_detected": False,
            "suspicious_files": [],
        }

        secret_patterns = {
            ".pem",
            ".key",
            ".p12",
            ".pfx",
            ".jks",
            ".keystore",
        }
        secret_names = {
            "id_rsa",
            "id_dsa",
            "id_ecdsa",
            "id_ed25519",
            "credentials.json",
            "service-account.json",
            "gcp-key.json",
            ".env",
        }

        for node in file_tree.nodes:
            fname = Path(node.path).name
            if node.extension in secret_patterns or fname in secret_names:
                security["secrets_detected"] = True
                security["suspicious_files"].append(node.path)

        return security

    # ------------------------------------------------------------------
    # Confidence scoring
    # ------------------------------------------------------------------

    @staticmethod
    def _calculate_confidence(
        language,
        framework,
        dependencies,
        entrypoint,
        port,
        warnings: list[str],
    ) -> float:
        score = 0.0

        if language.primary != "unknown":
            score += 0.25
        if framework.name is not None:
            score += 0.20
        if dependencies.manager != "unknown":
            score += 0.15
        if dependencies.lock_file_exists:
            score += 0.05
        if entrypoint.file is not None:
            score += 0.15
        if entrypoint.command is not None:
            score += 0.05
        if port.confidence in ("high", "medium"):
            score += 0.10
        if language.version is not None:
            score += 0.05

        penalty = min(len(warnings) * 0.05, 0.25)
        score = max(score - penalty, 0.0)

        return round(min(score, 1.0), 2)
