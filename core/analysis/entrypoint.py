from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from core.analysis.fingerprint import EntrypointInfo

logger = logging.getLogger(__name__)

# Conventional entrypoint file names per language
_CONVENTIONS: dict[str, list[str]] = {
    "python": ["main.py", "app.py", "server.py", "run.py", "wsgi.py", "asgi.py"],
    "javascript": ["index.js", "server.js", "app.js", "main.js", "src/index.js"],
    "typescript": ["index.ts", "server.ts", "app.ts", "main.ts", "src/index.ts"],
    "go": ["main.go", "cmd/main.go", "cmd/server/main.go"],
    "rust": ["src/main.rs"],
    "ruby": ["config.ru", "app.rb", "main.rb", "server.rb"],
    "csharp": ["Program.cs"],
    "elixir": ["lib/application.ex"],
}

# Framework-specific entrypoint overrides
_FRAMEWORK_ENTRYPOINTS: dict[str, dict[str, str | None]] = {
    "django": {"file": "manage.py", "command": "gunicorn config.wsgi:application"},
    "flask": {"file": "app.py", "command": "gunicorn app:app"},
    "fastapi": {"file": "main.py", "command": "uvicorn main:app --host 0.0.0.0"},
    "streamlit": {"file": "app.py", "command": "streamlit run app.py"},
    "nextjs": {"file": "package.json", "command": "npm start"},
    "nuxt": {"file": "package.json", "command": "npm start"},
    "express": {"file": "index.js", "command": "node index.js"},
    "nestjs": {"file": "src/main.ts", "command": "node dist/main"},
    "gin": {"file": "main.go", "command": "./app"},
    "fiber": {"file": "main.go", "command": "./app"},
    "echo": {"file": "main.go", "command": "./app"},
    "rails": {"file": "config.ru", "command": "bundle exec rails server -b 0.0.0.0 -p 3000"},
    "sinatra": {"file": "app.rb", "command": "ruby app.rb"},
    "hanami": {"file": "config/hanami.rb", "command": "bundle exec hanami server"},
    "aspnet_core": {"file": "Program.cs", "command": "dotnet run"},
    "blazor": {"file": "Program.cs", "command": "dotnet run"},
    "phoenix": {"file": "mix.exs", "command": "mix phx.server"},
    "plug": {"file": "mix.exs", "command": "mix run --no-halt"},
}

# Patterns that signal a file is a program entrypoint
_ENTRYPOINT_PATTERNS: dict[str, re.Pattern[str]] = {
    "python": re.compile(r'if\s+__name__\s*==\s*["\']__main__["\']'),
    "go": re.compile(r"func\s+main\s*\(\s*\)"),
    "rust": re.compile(r"fn\s+main\s*\(\s*\)"),
    "java": re.compile(r"public\s+static\s+void\s+main\s*\("),
}


class EntrypointDetector:

    def detect(
        self,
        root_path: str,
        language: str,
        framework_name: str | None,
    ) -> EntrypointInfo:
        root = Path(root_path)

        # 1. Config-based detection
        result = self._from_config(root, language)
        if result:
            return result

        # 2. Framework-based detection
        if framework_name:
            result = self._from_framework(root, framework_name)
            if result:
                return result

        # 3. Convention-based detection
        result = self._from_convention(root, language)
        if result:
            return result

        # 4. Pattern-based detection
        result = self._from_pattern(root, language)
        if result:
            return result

        return EntrypointInfo(file=None, command=None, detection_method="none")

    # ------------------------------------------------------------------
    # Strategy 1: config files
    # ------------------------------------------------------------------

    @staticmethod
    def _from_config(root: Path, language: str) -> EntrypointInfo | None:
        # package.json scripts.start
        pkg_json = root / "package.json"
        if pkg_json.is_file() and language in ("javascript", "typescript"):
            try:
                data = json.loads(pkg_json.read_text(errors="replace"))
                start = data.get("scripts", {}).get("start")
                main = data.get("main")
                if start:
                    return EntrypointInfo(
                        file=main or "package.json",
                        command=start,
                        detection_method="config",
                    )
            except (json.JSONDecodeError, OSError):
                pass

        # pyproject.toml [project.scripts] or [tool.poetry.scripts]
        pyproject = root / "pyproject.toml"
        if pyproject.is_file() and language == "python":
            try:
                text = pyproject.read_text(errors="replace")
                m = re.search(
                    r"\[(?:project\.scripts|tool\.poetry\.scripts)\]\s*\n([^\[]+)",
                    text,
                )
                if m:
                    first_line = m.group(1).strip().splitlines()[0]
                    if "=" in first_line:
                        cmd = first_line.split("=", 1)[1].strip().strip('"')
                        return EntrypointInfo(
                            file="pyproject.toml",
                            command=cmd,
                            detection_method="config",
                        )
            except OSError:
                pass

        # Procfile
        procfile = root / "Procfile"
        if procfile.is_file():
            try:
                for line in procfile.read_text(errors="replace").splitlines():
                    if line.strip().startswith("web:"):
                        cmd = line.split(":", 1)[1].strip()
                        return EntrypointInfo(
                            file="Procfile",
                            command=cmd,
                            detection_method="config",
                        )
            except OSError:
                pass

        # Ruby: Gemfile signals
        if language == "ruby":
            gemfile = root / "Gemfile"
            if gemfile.is_file():
                try:
                    text = gemfile.read_text(errors="replace")
                    if re.search(r"""gem\s+['"]rails['"]""", text):
                        for candidate in ("config.ru", "Procfile"):
                            if (root / candidate).is_file():
                                return EntrypointInfo(
                                    file=candidate,
                                    command="bundle exec rails server -b 0.0.0.0 -p 3000",
                                    detection_method="config",
                                )
                    if re.search(r"""gem\s+['"]sinatra['"]""", text) and (root / "app.rb").is_file():
                        return EntrypointInfo(
                            file="app.rb",
                            command="ruby app.rb",
                            detection_method="config",
                        )
                except OSError:
                    pass

        # C#: *.csproj detection
        if language == "csharp":
            for csproj in root.glob("*.csproj"):
                return EntrypointInfo(
                    file="Program.cs" if (root / "Program.cs").is_file() else csproj.name,
                    command="dotnet run",
                    detection_method="config",
                )

        # Elixir: mix.exs detection
        if language == "elixir":
            mix_exs = root / "mix.exs"
            if mix_exs.is_file():
                try:
                    text = mix_exs.read_text(errors="replace")
                    if re.search(r"""\{:phoenix\s*,""", text):
                        return EntrypointInfo(
                            file="mix.exs",
                            command="mix phx.server",
                            detection_method="config",
                        )
                    return EntrypointInfo(
                        file="mix.exs",
                        command="mix run --no-halt",
                        detection_method="config",
                    )
                except OSError:
                    pass

        return None

    # ------------------------------------------------------------------
    # Strategy 2: framework defaults
    # ------------------------------------------------------------------

    @staticmethod
    def _from_framework(root: Path, framework_name: str) -> EntrypointInfo | None:
        info = _FRAMEWORK_ENTRYPOINTS.get(framework_name)
        if not info:
            return None

        candidate = root / info["file"]
        if candidate.is_file():
            return EntrypointInfo(
                file=info["file"],
                command=info["command"],
                detection_method="framework",
            )
        return None

    # ------------------------------------------------------------------
    # Strategy 3: conventions
    # ------------------------------------------------------------------

    @staticmethod
    def _from_convention(root: Path, language: str) -> EntrypointInfo | None:
        candidates = _CONVENTIONS.get(language, [])
        for name in candidates:
            if (root / name).is_file():
                return EntrypointInfo(
                    file=name, command=None, detection_method="convention"
                )
        return None

    # ------------------------------------------------------------------
    # Strategy 4: pattern search
    # ------------------------------------------------------------------

    @staticmethod
    def _from_pattern(root: Path, language: str) -> EntrypointInfo | None:
        pattern = _ENTRYPOINT_PATTERNS.get(language)
        if not pattern:
            return None

        extensions = {
            "python": ".py",
            "go": ".go",
            "rust": ".rs",
            "java": ".java",
        }
        ext = extensions.get(language)
        if not ext:
            return None

        try:
            for fpath in sorted(root.rglob(f"*{ext}")):
                if any(
                    part in fpath.parts
                    for part in ("node_modules", ".git", "__pycache__", "venv", ".venv", "vendor")
                ):
                    continue
                if fpath.stat().st_size > 512_000:
                    continue
                try:
                    text = fpath.read_text(errors="replace")
                except OSError:
                    continue
                if pattern.search(text):
                    return EntrypointInfo(
                        file=str(fpath.relative_to(root)),
                        command=None,
                        detection_method="ast",
                    )
        except OSError:
            logger.debug("Pattern-based entrypoint scan failed", exc_info=True)

        return None
