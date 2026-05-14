from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from core.analysis.fingerprint import FileTree, FrameworkDetection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Framework signal definitions
# ---------------------------------------------------------------------------

PYTHON_FRAMEWORKS: dict[str, dict] = {
    "django": {
        "file_signals": ["manage.py"],
        "import_signals": ["django"],
        "default_port": 8000,
        "build_command": "pip install -r requirements.txt",
        "start_command": "gunicorn config.wsgi:application --bind 0.0.0.0:8000",
        "is_static": False,
    },
    "flask": {
        "file_signals": [],
        "import_signals": ["flask"],
        "default_port": 5000,
        "build_command": "pip install -r requirements.txt",
        "start_command": "gunicorn app:app --bind 0.0.0.0:5000",
        "is_static": False,
    },
    "fastapi": {
        "file_signals": [],
        "import_signals": ["fastapi"],
        "default_port": 8000,
        "build_command": "pip install -r requirements.txt",
        "start_command": "uvicorn main:app --host 0.0.0.0 --port 8000",
        "is_static": False,
    },
    "streamlit": {
        "file_signals": [".streamlit/config.toml"],
        "import_signals": ["streamlit"],
        "default_port": 8501,
        "build_command": "pip install -r requirements.txt",
        "start_command": "streamlit run app.py --server.port 8501 --server.address 0.0.0.0",
        "is_static": False,
    },
}

NODE_FRAMEWORKS: dict[str, dict] = {
    "nextjs": {
        "file_signals": ["next.config.js", "next.config.mjs", "next.config.ts"],
        "package_signals": ["next"],
        "default_port": 3000,
        "build_command": "npm run build",
        "start_command": "npm start",
        "is_static": False,
    },
    "nuxt": {
        "file_signals": ["nuxt.config.js", "nuxt.config.ts"],
        "package_signals": ["nuxt"],
        "default_port": 3000,
        "build_command": "npm run build",
        "start_command": "npm start",
        "is_static": False,
    },
    "express": {
        "file_signals": [],
        "package_signals": ["express"],
        "default_port": 3000,
        "build_command": "npm install",
        "start_command": "node index.js",
        "is_static": False,
    },
    "nestjs": {
        "file_signals": ["nest-cli.json"],
        "package_signals": ["@nestjs/core"],
        "default_port": 3000,
        "build_command": "npm run build",
        "start_command": "node dist/main",
        "is_static": False,
    },
    "vite_react": {
        "file_signals": ["vite.config.js", "vite.config.ts"],
        "package_signals": ["vite", "react"],
        "default_port": 5173,
        "build_command": "npm run build",
        "start_command": "npx serve dist",
        "is_static": True,
    },
}

GO_FRAMEWORKS: dict[str, dict] = {
    "gin": {
        "file_signals": [],
        "import_signals": ["github.com/gin-gonic/gin"],
        "default_port": 8080,
        "build_command": "go build -o app .",
        "start_command": "./app",
        "is_static": False,
    },
    "fiber": {
        "file_signals": [],
        "import_signals": ["github.com/gofiber/fiber"],
        "default_port": 3000,
        "build_command": "go build -o app .",
        "start_command": "./app",
        "is_static": False,
    },
    "echo": {
        "file_signals": [],
        "import_signals": ["github.com/labstack/echo"],
        "default_port": 8080,
        "build_command": "go build -o app .",
        "start_command": "./app",
        "is_static": False,
    },
}

RUBY_FRAMEWORKS: dict[str, dict] = {
    "rails": {
        "file_signals": ["Rakefile", "config/routes.rb", "config/application.rb", "bin/rails"],
        "gem_signals": ["rails"],
        "default_port": 3000,
        "build_command": None,
        "start_command": "bundle exec rails server -b 0.0.0.0 -p 3000",
        "is_static": False,
    },
    "sinatra": {
        "file_signals": [],
        "gem_signals": ["sinatra"],
        "default_port": 4567,
        "build_command": None,
        "start_command": "ruby app.rb",
        "is_static": False,
    },
    "hanami": {
        "file_signals": ["config/hanami.rb"],
        "gem_signals": ["hanami"],
        "default_port": 2300,
        "build_command": None,
        "start_command": "bundle exec hanami server",
        "is_static": False,
    },
}

CSHARP_FRAMEWORKS: dict[str, dict] = {
    "aspnet_core": {
        "file_signals": ["Program.cs", "Startup.cs", "appsettings.json"],
        "package_signals": ["Microsoft.AspNetCore"],
        "default_port": 5000,
        "build_command": "dotnet publish -c Release -o out",
        "start_command": "dotnet out/{assembly}.dll",
        "is_static": False,
    },
    "blazor": {
        "file_signals": ["_Imports.razor", "App.razor"],
        "package_signals": ["Microsoft.AspNetCore.Components"],
        "default_port": 5000,
        "build_command": "dotnet publish -c Release -o out",
        "start_command": "dotnet out/{assembly}.dll",
        "is_static": False,
    },
}

ELIXIR_FRAMEWORKS: dict[str, dict] = {
    "phoenix": {
        "file_signals": ["config/config.exs", "lib/*_web/"],
        "dep_signals": ["phoenix"],
        "default_port": 4000,
        "build_command": "mix deps.get && mix compile && mix assets.deploy",
        "start_command": "mix phx.server",
        "is_static": False,
    },
    "plug": {
        "file_signals": [],
        "dep_signals": ["plug", "plug_cowboy"],
        "default_port": 4000,
        "build_command": "mix deps.get && mix compile",
        "start_command": "mix run --no-halt",
        "is_static": False,
    },
}


class FrameworkDetector:

    def detect(
        self, file_tree: FileTree, language: str, root_path: str
    ) -> FrameworkDetection:
        try:
            if language == "python":
                return self._detect_python(file_tree, root_path)
            if language in ("javascript", "typescript"):
                return self._detect_node(file_tree, root_path)
            if language == "go":
                return self._detect_go(file_tree, root_path)
            if language == "ruby":
                return self._detect_ruby(file_tree, root_path)
            if language == "csharp":
                return self._detect_csharp(file_tree, root_path)
            if language == "elixir":
                return self._detect_elixir(file_tree, root_path)
        except Exception:
            logger.warning("Framework detection failed for %s", language, exc_info=True)

        return FrameworkDetection(
            name=None,
            version=None,
            is_static=False,
            default_port=None,
            build_command=None,
            start_command=None,
        )

    # ------------------------------------------------------------------
    # Python
    # ------------------------------------------------------------------

    def _detect_python(self, file_tree: FileTree, root_path: str) -> FrameworkDetection:
        root = Path(root_path)
        file_names = {Path(n.path).name for n in file_tree.nodes}

        installed_packages = self._read_python_packages(root)
        source_imports = self._collect_python_imports(root, file_tree)

        for name, signals in PYTHON_FRAMEWORKS.items():
            if any(f in file_names for f in signals["file_signals"]):
                return self._build_detection(name, signals, root, "python")
            if any(imp in source_imports for imp in signals["import_signals"]):
                return self._build_detection(name, signals, root, "python")
            if any(pkg in installed_packages for pkg in signals["import_signals"]):
                return self._build_detection(name, signals, root, "python")

        return self._empty_detection()

    @staticmethod
    def _read_python_packages(root: Path) -> set[str]:
        packages: set[str] = set()

        req = root / "requirements.txt"
        if req.is_file():
            for line in req.read_text(errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("-"):
                    continue
                pkg = re.split(r"[><=!~\[]", line, maxsplit=1)[0].strip().lower()
                if pkg:
                    packages.add(pkg)

        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            text = pyproject.read_text(errors="replace")
            for m in re.finditer(r'"([a-zA-Z0-9_-]+)', text):
                packages.add(m.group(1).lower())

        return packages

    @staticmethod
    def _collect_python_imports(root: Path, file_tree: FileTree) -> set[str]:
        imports: set[str] = set()
        for node in file_tree.nodes:
            if node.extension != ".py":
                continue
            fpath = root / node.path
            if not fpath.is_file() or node.size_bytes > 512_000:
                continue
            try:
                text = fpath.read_text(errors="replace")
            except OSError:
                continue
            for m in re.finditer(
                r"^\s*(?:from|import)\s+([a-zA-Z_][a-zA-Z0-9_]*)", text, re.MULTILINE
            ):
                imports.add(m.group(1).lower())
        return imports

    # ------------------------------------------------------------------
    # Node.js / TypeScript
    # ------------------------------------------------------------------

    def _detect_node(self, file_tree: FileTree, root_path: str) -> FrameworkDetection:
        root = Path(root_path)
        file_names = {Path(n.path).name for n in file_tree.nodes}

        pkg_json = root / "package.json"
        all_deps: set[str] = set()
        if pkg_json.is_file():
            try:
                data = json.loads(pkg_json.read_text(errors="replace"))
                all_deps.update(data.get("dependencies", {}).keys())
                all_deps.update(data.get("devDependencies", {}).keys())
            except (json.JSONDecodeError, AttributeError):
                pass

        for name, signals in NODE_FRAMEWORKS.items():
            if any(f in file_names for f in signals["file_signals"]):
                return self._build_detection(name, signals, root, "node")
            if all(pkg in all_deps for pkg in signals["package_signals"]):
                return self._build_detection(name, signals, root, "node")

        return self._empty_detection()

    # ------------------------------------------------------------------
    # Go
    # ------------------------------------------------------------------

    def _detect_go(self, file_tree: FileTree, root_path: str) -> FrameworkDetection:
        root = Path(root_path)
        go_imports = self._collect_go_imports(root, file_tree)

        for name, signals in GO_FRAMEWORKS.items():
            if any(imp in go_imports for imp in signals["import_signals"]):
                return self._build_detection(name, signals, root, "go")

        return self._empty_detection()

    @staticmethod
    def _collect_go_imports(root: Path, file_tree: FileTree) -> set[str]:
        imports: set[str] = set()
        for node in file_tree.nodes:
            if node.extension != ".go":
                continue
            fpath = root / node.path
            if not fpath.is_file() or node.size_bytes > 512_000:
                continue
            try:
                text = fpath.read_text(errors="replace")
            except OSError:
                continue
            for m in re.finditer(r'"([^"]+)"', text):
                imports.add(m.group(1))
        return imports

    # ------------------------------------------------------------------
    # Ruby
    # ------------------------------------------------------------------

    def _detect_ruby(self, file_tree: FileTree, root_path: str) -> FrameworkDetection:
        root = Path(root_path)
        file_paths = {n.path for n in file_tree.nodes}
        file_names = {Path(n.path).name for n in file_tree.nodes}

        installed_gems = self._read_gemfile_gems(root)

        for name, signals in RUBY_FRAMEWORKS.items():
            for sig in signals["file_signals"]:
                if sig in file_names or sig in file_paths:
                    return self._build_detection(name, signals, root, "ruby")
            if any(gem in installed_gems for gem in signals["gem_signals"]):
                return self._build_detection(name, signals, root, "ruby")

        return self._empty_detection()

    @staticmethod
    def _read_gemfile_gems(root: Path) -> set[str]:
        gems: set[str] = set()
        gemfile = root / "Gemfile"
        if not gemfile.is_file():
            return gems
        try:
            text = gemfile.read_text(errors="replace")
        except OSError:
            return gems
        for m in re.finditer(r"""gem\s+['"]([a-zA-Z0-9_-]+)['"]""", text):
            gems.add(m.group(1).lower())
        return gems

    # ------------------------------------------------------------------
    # C# / .NET
    # ------------------------------------------------------------------

    def _detect_csharp(self, file_tree: FileTree, root_path: str) -> FrameworkDetection:
        root = Path(root_path)
        file_names = {Path(n.path).name for n in file_tree.nodes}

        nuget_packages = self._read_csproj_packages(root, file_tree)

        for name, signals in CSHARP_FRAMEWORKS.items():
            if any(f in file_names for f in signals["file_signals"]):
                return self._build_detection(name, signals, root, "csharp")
            if any(
                pkg.startswith(prefix) for pkg in nuget_packages
                for prefix in signals["package_signals"]
            ):
                return self._build_detection(name, signals, root, "csharp")

        return self._empty_detection()

    @staticmethod
    def _read_csproj_packages(root: Path, file_tree: FileTree) -> set[str]:
        packages: set[str] = set()
        for node in file_tree.nodes:
            if not node.path.endswith(".csproj"):
                continue
            fpath = root / node.path
            if not fpath.is_file():
                continue
            try:
                text = fpath.read_text(errors="replace")
            except OSError:
                continue
            for m in re.finditer(
                r'<PackageReference\s+Include="([^"]+)"', text, re.IGNORECASE
            ):
                packages.add(m.group(1))
        return packages

    # ------------------------------------------------------------------
    # Elixir
    # ------------------------------------------------------------------

    def _detect_elixir(self, file_tree: FileTree, root_path: str) -> FrameworkDetection:
        root = Path(root_path)
        file_paths = {n.path for n in file_tree.nodes}
        file_names = {Path(n.path).name for n in file_tree.nodes}

        mix_deps = self._read_mix_deps(root)

        for name, signals in ELIXIR_FRAMEWORKS.items():
            for sig in signals["file_signals"]:
                if sig.endswith("/"):
                    if any(p.startswith(sig) or ("/" + sig) in p for p in file_paths):
                        return self._build_detection(name, signals, root, "elixir")
                elif sig in file_names or sig in file_paths:
                    return self._build_detection(name, signals, root, "elixir")
            if any(dep in mix_deps for dep in signals["dep_signals"]):
                return self._build_detection(name, signals, root, "elixir")

        return self._empty_detection()

    @staticmethod
    def _read_mix_deps(root: Path) -> set[str]:
        deps: set[str] = set()
        mix_exs = root / "mix.exs"
        if not mix_exs.is_file():
            return deps
        try:
            text = mix_exs.read_text(errors="replace")
        except OSError:
            return deps
        for m in re.finditer(r"""\{:(\w+)\s*,""", text):
            deps.add(m.group(1).lower())
        return deps

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_detection(
        name: str, signals: dict, root: Path, lang_family: str
    ) -> FrameworkDetection:
        version: str | None = None
        if lang_family == "node":
            pkg_json = root / "package.json"
            if pkg_json.is_file():
                try:
                    data = json.loads(pkg_json.read_text(errors="replace"))
                    deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                    for pkg_signal in signals.get("package_signals", []):
                        if pkg_signal in deps:
                            version = deps[pkg_signal]
                            break
                except (json.JSONDecodeError, AttributeError):
                    pass
        elif lang_family == "ruby":
            gemfile = root / "Gemfile"
            if gemfile.is_file():
                try:
                    text = gemfile.read_text(errors="replace")
                    for gem_sig in signals.get("gem_signals", []):
                        m = re.search(
                            rf"""gem\s+['"]{re.escape(gem_sig)}['"]\s*,\s*['"]([^'"]+)['"]""",
                            text,
                        )
                        if m:
                            version = m.group(1)
                            break
                except OSError:
                    pass
        elif lang_family == "csharp":
            for csproj in root.glob("*.csproj"):
                try:
                    text = csproj.read_text(errors="replace")
                    for pkg_signal in signals.get("package_signals", []):
                        m = re.search(
                            rf'<PackageReference\s+Include="{re.escape(pkg_signal)}[^"]*"\s+'
                            rf'Version="([^"]+)"',
                            text,
                            re.IGNORECASE,
                        )
                        if m:
                            version = m.group(1)
                            break
                    if version:
                        break
                except OSError:
                    pass
        elif lang_family == "elixir":
            mix_exs = root / "mix.exs"
            if mix_exs.is_file():
                try:
                    text = mix_exs.read_text(errors="replace")
                    for dep_sig in signals.get("dep_signals", []):
                        m = re.search(
                            rf"""\{{:{re.escape(dep_sig)}\s*,\s*"([^"]+)"\}}""",
                            text,
                        )
                        if m:
                            version = m.group(1)
                            break
                except OSError:
                    pass

        return FrameworkDetection(
            name=name,
            version=version,
            is_static=signals.get("is_static", False),
            default_port=signals.get("default_port"),
            build_command=signals.get("build_command"),
            start_command=signals.get("start_command"),
        )

    @staticmethod
    def _empty_detection() -> FrameworkDetection:
        return FrameworkDetection(
            name=None,
            version=None,
            is_static=False,
            default_port=None,
            build_command=None,
            start_command=None,
        )
