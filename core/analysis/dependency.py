from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from core.analysis.fingerprint import Dependency, DependencyInfo

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Native / system-level dependencies required by popular packages
# ---------------------------------------------------------------------------

NATIVE_DEPS: dict[str, dict[str, dict[str, list[str]]]] = {
    "python": {
        "numpy": {
            "alpine": ["openblas-dev"],
            "debian": ["libopenblas-dev"],
        },
        "pillow": {
            "alpine": ["jpeg-dev", "zlib-dev", "freetype-dev"],
            "debian": ["libjpeg-dev", "zlib1g-dev", "libfreetype6-dev"],
        },
        "psycopg2": {
            "alpine": ["postgresql-dev", "gcc", "musl-dev"],
            "debian": ["libpq-dev"],
        },
        "cryptography": {
            "alpine": ["gcc", "musl-dev", "openssl-dev", "libffi-dev"],
            "debian": ["libssl-dev", "libffi-dev"],
        },
        "bcrypt": {
            "alpine": ["gcc", "musl-dev", "libffi-dev"],
            "debian": ["libffi-dev"],
        },
    },
    "node": {
        "sharp": {
            "alpine": ["vips-dev"],
            "debian": ["libvips-dev"],
        },
        "canvas": {
            "alpine": ["cairo-dev", "pango-dev", "jpeg-dev"],
            "debian": ["libcairo2-dev", "libpango1.0-dev"],
        },
        "bcrypt": {
            "alpine": ["python3", "make", "g++"],
            "debian": ["python3", "make", "g++"],
        },
    },
    "ruby": {
        "nokogiri": {
            "alpine": ["libxml2-dev", "libxslt-dev"],
            "debian": ["libxml2-dev", "libxslt-dev"],
        },
        "pg": {
            "alpine": ["postgresql-dev"],
            "debian": ["libpq-dev"],
        },
        "mysql2": {
            "alpine": ["mariadb-dev"],
            "debian": ["libmariadb-dev"],
        },
    },
}


class DependencyAnalyzer:

    def analyze(self, root_path: str, language: str) -> DependencyInfo:
        root = Path(root_path)

        if language == "python":
            return self._analyze_python(root)
        if language in ("javascript", "typescript"):
            return self._analyze_node(root)
        if language == "go":
            return self._analyze_go(root)
        if language == "ruby":
            return self._analyze_ruby(root)
        if language == "csharp":
            return self._analyze_csharp(root)
        if language == "elixir":
            return self._analyze_elixir(root)

        return DependencyInfo(
            manager="unknown",
            lock_file_exists=False,
            runtime_deps=[],
            dev_deps=[],
            system_packages_needed={},
            language_version=None,
        )

    # ------------------------------------------------------------------
    # Python
    # ------------------------------------------------------------------

    def _analyze_python(self, root: Path) -> DependencyInfo:
        manager = "pip"
        lock_file_exists = False
        runtime_deps: list[Dependency] = []
        dev_deps: list[Dependency] = []
        language_version: str | None = None

        if (root / "Pipfile").is_file():
            manager = "pipenv"
            lock_file_exists = (root / "Pipfile.lock").is_file()
            runtime_deps, dev_deps = self._parse_pipfile(root)
        elif (root / "pyproject.toml").is_file():
            manager = "pyproject"
            lock_file_exists = any(
                (root / lf).is_file()
                for lf in ("poetry.lock", "pdm.lock", "uv.lock")
            )
            runtime_deps, dev_deps = self._parse_pyproject(root)
            language_version = self._pyproject_python_version(root)
        elif (root / "requirements.txt").is_file():
            manager = "pip"
            lock_file_exists = False
            runtime_deps = self._parse_requirements(root / "requirements.txt")

        if not dev_deps and (root / "requirements-dev.txt").is_file():
            dev_deps = self._parse_requirements(root / "requirements-dev.txt")

        sys_pkgs = self._resolve_system_packages("python", runtime_deps + dev_deps)

        return DependencyInfo(
            manager=manager,
            lock_file_exists=lock_file_exists,
            runtime_deps=runtime_deps,
            dev_deps=dev_deps,
            system_packages_needed=sys_pkgs,
            language_version=language_version,
        )

    @staticmethod
    def _parse_requirements(path: Path) -> list[Dependency]:
        deps: list[Dependency] = []
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return deps
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            parts = re.split(r"([><=!~\[;])", line, maxsplit=1)
            name = parts[0].strip().lower()
            constraint = line[len(parts[0]):].strip() or None
            if constraint and constraint.startswith("["):
                constraint = None
            if name:
                deps.append(Dependency(name=name, version_constraint=constraint, is_native=False))
        return deps

    def _parse_pyproject(self, root: Path) -> tuple[list[Dependency], list[Dependency]]:
        runtime: list[Dependency] = []
        dev: list[Dependency] = []
        try:
            text = (root / "pyproject.toml").read_text(errors="replace")
        except OSError:
            return runtime, dev

        in_deps = False
        in_dev = False
        for line in text.splitlines():
            stripped = line.strip()
            if re.match(r"\[.*dependencies\]", stripped) and "dev" not in stripped.lower() and "optional" not in stripped.lower():
                in_deps = True
                in_dev = False
                continue
            if re.match(r"\[.*dev.*\]", stripped, re.IGNORECASE):
                in_deps = False
                in_dev = True
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                in_deps = False
                in_dev = False
                continue

            m = re.match(r'"([a-zA-Z0-9_][a-zA-Z0-9_.~-]*)\s*([><=!~].*?)?"', stripped)
            if not m and in_deps:
                m = re.match(r'([a-zA-Z0-9_][a-zA-Z0-9_.~-]*)\s*=\s*"([^"]*)"', stripped)
            if m:
                name = m.group(1).lower()
                constraint = m.group(2) if m.lastindex and m.lastindex >= 2 else None
                dep = Dependency(name=name, version_constraint=constraint, is_native=False)
                if in_dev:
                    dev.append(dep)
                elif in_deps:
                    runtime.append(dep)

        return runtime, dev

    @staticmethod
    def _parse_pipfile(root: Path) -> tuple[list[Dependency], list[Dependency]]:
        runtime: list[Dependency] = []
        dev: list[Dependency] = []
        try:
            text = (root / "Pipfile").read_text(errors="replace")
        except OSError:
            return runtime, dev

        section: str | None = None
        for line in text.splitlines():
            stripped = line.strip()
            if stripped == "[packages]":
                section = "packages"
                continue
            if stripped == "[dev-packages]":
                section = "dev-packages"
                continue
            if stripped.startswith("["):
                section = None
                continue
            if section and "=" in stripped:
                name = stripped.split("=", 1)[0].strip().strip('"').lower()
                version = stripped.split("=", 1)[1].strip().strip('"')
                constraint = version if version != "*" else None
                dep = Dependency(name=name, version_constraint=constraint, is_native=False)
                if section == "dev-packages":
                    dev.append(dep)
                else:
                    runtime.append(dep)

        return runtime, dev

    @staticmethod
    def _pyproject_python_version(root: Path) -> str | None:
        try:
            text = (root / "pyproject.toml").read_text(errors="replace")
        except OSError:
            return None
        m = re.search(r'requires-python\s*=\s*"([^"]+)"', text)
        return m.group(1).strip() if m else None

    # ------------------------------------------------------------------
    # Node.js / TypeScript
    # ------------------------------------------------------------------

    def _analyze_node(self, root: Path) -> DependencyInfo:
        pkg_json = root / "package.json"
        if not pkg_json.is_file():
            return DependencyInfo(
                manager="npm",
                lock_file_exists=False,
                runtime_deps=[],
                dev_deps=[],
                system_packages_needed={},
                language_version=None,
            )

        try:
            data = json.loads(pkg_json.read_text(errors="replace"))
        except (json.JSONDecodeError, OSError):
            data = {}

        manager = "npm"
        if (root / "yarn.lock").is_file():
            manager = "yarn"
        elif (root / "pnpm-lock.yaml").is_file():
            manager = "pnpm"
        elif (root / "bun.lockb").is_file():
            manager = "bun"

        lock_file_exists = any(
            (root / lf).is_file()
            for lf in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb")
        )

        runtime_deps = [
            Dependency(name=k, version_constraint=v, is_native=False)
            for k, v in data.get("dependencies", {}).items()
        ]
        dev_deps = [
            Dependency(name=k, version_constraint=v, is_native=False)
            for k, v in data.get("devDependencies", {}).items()
        ]

        language_version = data.get("engines", {}).get("node")

        sys_pkgs = self._resolve_system_packages("node", runtime_deps + dev_deps)

        return DependencyInfo(
            manager=manager,
            lock_file_exists=lock_file_exists,
            runtime_deps=runtime_deps,
            dev_deps=dev_deps,
            system_packages_needed=sys_pkgs,
            language_version=language_version,
        )

    # ------------------------------------------------------------------
    # Go
    # ------------------------------------------------------------------

    def _analyze_go(self, root: Path) -> DependencyInfo:
        gomod = root / "go.mod"
        if not gomod.is_file():
            return DependencyInfo(
                manager="go_modules",
                lock_file_exists=False,
                runtime_deps=[],
                dev_deps=[],
                system_packages_needed={},
                language_version=None,
            )

        try:
            text = gomod.read_text(errors="replace")
        except OSError:
            text = ""

        language_version: str | None = None
        deps: list[Dependency] = []
        in_require = False

        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("go "):
                language_version = stripped.split(maxsplit=1)[1]
            if stripped == "require (":
                in_require = True
                continue
            if stripped == ")":
                in_require = False
                continue
            if in_require and stripped:
                parts = stripped.split()
                if len(parts) >= 2 and not parts[0].startswith("//"):
                    deps.append(
                        Dependency(
                            name=parts[0],
                            version_constraint=parts[1],
                            is_native=False,
                        )
                    )

        lock_file_exists = (root / "go.sum").is_file()

        return DependencyInfo(
            manager="go_modules",
            lock_file_exists=lock_file_exists,
            runtime_deps=deps,
            dev_deps=[],
            system_packages_needed={},
            language_version=language_version,
        )

    # ------------------------------------------------------------------
    # Ruby
    # ------------------------------------------------------------------

    def _analyze_ruby(self, root: Path) -> DependencyInfo:
        gemfile = root / "Gemfile"
        if not gemfile.is_file():
            return DependencyInfo(
                manager="bundler",
                lock_file_exists=False,
                runtime_deps=[],
                dev_deps=[],
                system_packages_needed={},
                language_version=None,
            )

        runtime_deps, dev_deps = self._parse_gemfile(gemfile)
        lock_file_exists = (root / "Gemfile.lock").is_file()
        language_version = self._detect_ruby_version(root, gemfile)

        sys_pkgs = self._resolve_system_packages("ruby", runtime_deps + dev_deps)

        return DependencyInfo(
            manager="bundler",
            lock_file_exists=lock_file_exists,
            runtime_deps=runtime_deps,
            dev_deps=dev_deps,
            system_packages_needed=sys_pkgs,
            language_version=language_version,
        )

    @staticmethod
    def _parse_gemfile(gemfile: Path) -> tuple[list[Dependency], list[Dependency]]:
        runtime: list[Dependency] = []
        dev: list[Dependency] = []
        try:
            text = gemfile.read_text(errors="replace")
        except OSError:
            return runtime, dev

        in_dev_group = False
        group_depth = 0

        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue

            if re.match(r"group\s+.*:development.*\bdo\b", stripped) or re.match(
                r"group\s+.*:test.*\bdo\b", stripped
            ):
                in_dev_group = True
                group_depth = 1
                continue
            if in_dev_group:
                group_depth += stripped.count(" do") + stripped.count(" do\n")
                if stripped == "end":
                    group_depth -= 1
                    if group_depth <= 0:
                        in_dev_group = False
                    continue

            m = re.match(
                r"""gem\s+['"]([a-zA-Z0-9_-]+)['"]\s*(?:,\s*['"]([^'"]*)['"]\s*)?""",
                stripped,
            )
            if m:
                name = m.group(1).lower()
                constraint = m.group(2) if m.lastindex and m.lastindex >= 2 else None
                dep = Dependency(name=name, version_constraint=constraint, is_native=False)
                if in_dev_group:
                    dev.append(dep)
                else:
                    runtime.append(dep)

        return runtime, dev

    @staticmethod
    def _detect_ruby_version(root: Path, gemfile: Path) -> str | None:
        ruby_version_file = root / ".ruby-version"
        if ruby_version_file.is_file():
            try:
                version = ruby_version_file.read_text(errors="replace").strip()
                if version:
                    return version
            except OSError:
                pass

        try:
            text = gemfile.read_text(errors="replace")
            m = re.search(r"""ruby\s+['"]([^'"]+)['"]""", text)
            if m:
                return m.group(1)
        except OSError:
            pass

        return None

    # ------------------------------------------------------------------
    # C# / .NET
    # ------------------------------------------------------------------

    def _analyze_csharp(self, root: Path) -> DependencyInfo:
        csproj_files = list(root.glob("*.csproj"))
        if not csproj_files:
            csproj_files = list(root.rglob("*.csproj"))

        if not csproj_files:
            return DependencyInfo(
                manager="nuget",
                lock_file_exists=False,
                runtime_deps=[],
                dev_deps=[],
                system_packages_needed={},
                language_version=None,
            )

        runtime_deps: list[Dependency] = []
        language_version: str | None = None

        for csproj in csproj_files:
            deps, tfm = self._parse_csproj(csproj)
            runtime_deps.extend(deps)
            if tfm and not language_version:
                language_version = tfm

        lock_file_exists = any(
            (root / lf).is_file()
            for lf in ("packages.lock.json", "obj/project.assets.json")
        )

        return DependencyInfo(
            manager="nuget",
            lock_file_exists=lock_file_exists,
            runtime_deps=runtime_deps,
            dev_deps=[],
            system_packages_needed={},
            language_version=language_version,
        )

    @staticmethod
    def _parse_csproj(csproj: Path) -> tuple[list[Dependency], str | None]:
        deps: list[Dependency] = []
        target_framework: str | None = None
        try:
            text = csproj.read_text(errors="replace")
        except OSError:
            return deps, target_framework

        for m in re.finditer(
            r'<PackageReference\s+Include="([^"]+)"(?:\s+Version="([^"]*)")?',
            text,
            re.IGNORECASE,
        ):
            name = m.group(1)
            version = m.group(2) if m.lastindex and m.lastindex >= 2 else None
            deps.append(Dependency(name=name, version_constraint=version, is_native=False))

        m = re.search(r"<TargetFramework>([^<]+)</TargetFramework>", text, re.IGNORECASE)
        if m:
            target_framework = m.group(1).strip()

        return deps, target_framework

    # ------------------------------------------------------------------
    # Elixir
    # ------------------------------------------------------------------

    def _analyze_elixir(self, root: Path) -> DependencyInfo:
        mix_exs = root / "mix.exs"
        if not mix_exs.is_file():
            return DependencyInfo(
                manager="mix",
                lock_file_exists=False,
                runtime_deps=[],
                dev_deps=[],
                system_packages_needed={},
                language_version=None,
            )

        runtime_deps, dev_deps = self._parse_mix_exs(mix_exs)
        lock_file_exists = (root / "mix.lock").is_file()
        language_version = self._detect_elixir_version(root, mix_exs)

        return DependencyInfo(
            manager="mix",
            lock_file_exists=lock_file_exists,
            runtime_deps=runtime_deps,
            dev_deps=dev_deps,
            system_packages_needed={},
            language_version=language_version,
        )

    @staticmethod
    def _parse_mix_exs(mix_exs: Path) -> tuple[list[Dependency], list[Dependency]]:
        runtime: list[Dependency] = []
        dev: list[Dependency] = []
        try:
            text = mix_exs.read_text(errors="replace")
        except OSError:
            return runtime, dev

        for m in re.finditer(
            r"""\{:(\w+)\s*,\s*"([^"]*)"(?:\s*,\s*([^}]*))?\}""", text
        ):
            name = m.group(1).lower()
            constraint = m.group(2)
            options = m.group(3) or ""
            dep = Dependency(name=name, version_constraint=constraint, is_native=False)
            if "only:" in options and (":dev" in options or ":test" in options):
                dev.append(dep)
            else:
                runtime.append(dep)

        for m in re.finditer(r"""\{:(\w+)\s*,\s*[^"}][^}]*\}""", text):
            name = m.group(1).lower()
            if not any(d.name == name for d in runtime + dev):
                runtime.append(
                    Dependency(name=name, version_constraint=None, is_native=False)
                )

        return runtime, dev

    @staticmethod
    def _detect_elixir_version(root: Path, mix_exs: Path) -> str | None:
        elixir_version_file = root / ".elixir-version"
        if elixir_version_file.is_file():
            try:
                version = elixir_version_file.read_text(errors="replace").strip()
                if version:
                    return version
            except OSError:
                pass

        try:
            text = mix_exs.read_text(errors="replace")
            m = re.search(r"""elixir:\s*"([^"]+)\"""", text)
            if m:
                return m.group(1)
        except OSError:
            pass

        return None

    # ------------------------------------------------------------------
    # System package resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_system_packages(
        lang_family: str, deps: list[Dependency]
    ) -> dict[str, list[str]]:
        native_map = NATIVE_DEPS.get(lang_family, {})
        alpine: list[str] = []
        debian: list[str] = []

        for dep in deps:
            entry = native_map.get(dep.name.lower())
            if entry:
                dep.is_native = True
                for pkg in entry.get("alpine", []):
                    if pkg not in alpine:
                        alpine.append(pkg)
                for pkg in entry.get("debian", []):
                    if pkg not in debian:
                        debian.append(pkg)

        if not alpine and not debian:
            return {}
        return {"alpine": alpine, "debian": debian}
