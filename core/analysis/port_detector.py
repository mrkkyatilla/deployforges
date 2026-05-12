from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from core.analysis.fingerprint import (
    EntrypointInfo,
    FrameworkDetection,
    PortInfo,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Regex patterns to extract port numbers from source code
# ---------------------------------------------------------------------------

PORT_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "python": [
        re.compile(r"\.run\(.*port\s*=\s*(\d+)"),
        re.compile(r"uvicorn.*--port\s+(\d+)"),
        re.compile(r"gunicorn.*:(\d+)"),
        re.compile(r"PORT\s*=\s*(\d+)"),
    ],
    "javascript": [
        re.compile(r"\.listen\((\d+)"),
        re.compile(r"PORT\s*\|\|\s*(\d+)"),
        re.compile(r"port\s*[:=]\s*(\d+)"),
    ],
    "typescript": [
        re.compile(r"\.listen\((\d+)"),
        re.compile(r"PORT\s*\|\|\s*(\d+)"),
        re.compile(r"port\s*[:=]\s*(\d+)"),
    ],
    "go": [
        re.compile(r'ListenAndServe\(".*:(\d+)"'),
        re.compile(r'Addr\s*=\s*".*:(\d+)"'),
        re.compile(r"\.Run\(\":(\d+)\"\)"),
    ],
    "ruby": [
        re.compile(r"set\s+:port\s*,\s*(\d+)"),
        re.compile(r"-p\s+(\d+)"),
        re.compile(r"PORT\s*=\s*(\d+)"),
        re.compile(r"port\s*[:=]\s*(\d+)"),
    ],
    "csharp": [
        re.compile(r'"applicationUrl":\s*"https?://[^:]+:(\d+)"'),
        re.compile(r"\.UseUrls\(.*:(\d+)"),
        re.compile(r"ASPNETCORE_URLS.*:(\d+)"),
    ],
    "elixir": [
        re.compile(r"port:\s*(\d+)"),
        re.compile(r"http:\s*\[port:\s*(\d+)\]"),
        re.compile(r"PORT.*?(\d{4,5})"),
    ],
}

_LANGUAGE_DEFAULT_PORTS: dict[str, int] = {
    "python": 8000,
    "javascript": 3000,
    "typescript": 3000,
    "go": 8080,
    "rust": 8080,
    "java": 8080,
    "php": 8080,
    "ruby": 3000,
    "csharp": 5000,
    "elixir": 4000,
}


class PortDetector:

    def detect(
        self,
        root_path: str,
        language: str,
        framework: FrameworkDetection,
        entrypoint: EntrypointInfo,
    ) -> PortInfo:
        # 1. Framework default with high confidence
        if framework.default_port is not None:
            return PortInfo(
                value=framework.default_port,
                detection_method="framework_default",
                confidence="high",
            )

        root = Path(root_path)

        # 2. Scan entrypoint file for port patterns
        if entrypoint.file:
            result = self._scan_file(root / entrypoint.file, language)
            if result is not None:
                return PortInfo(
                    value=result, detection_method="code", confidence="high"
                )

        # 3. Scan common config files
        result = self._scan_config_files(root, language)
        if result is not None:
            return PortInfo(
                value=result, detection_method="config", confidence="medium"
            )

        # 4. Broad scan of source files
        result = self._scan_source_files(root, language)
        if result is not None:
            return PortInfo(
                value=result, detection_method="code", confidence="low"
            )

        # 5. Language default fallback
        default = _LANGUAGE_DEFAULT_PORTS.get(language, 8080)
        return PortInfo(
            value=default, detection_method="framework_default", confidence="low"
        )

    # ------------------------------------------------------------------
    # Scanning helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _scan_file(filepath: Path, language: str) -> int | None:
        if not filepath.is_file():
            return None
        patterns = PORT_PATTERNS.get(language, [])
        if not patterns:
            return None
        try:
            text = filepath.read_text(errors="replace")
        except OSError:
            return None
        for pat in patterns:
            m = pat.search(text)
            if m:
                try:
                    port = int(m.group(1))
                    if 1024 <= port <= 65535:
                        return port
                except (ValueError, IndexError):
                    continue
        return None

    def _scan_config_files(self, root: Path, language: str) -> int | None:
        # docker-compose.yml port mapping
        for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
            compose = root / name
            if compose.is_file():
                try:
                    text = compose.read_text(errors="replace")
                    m = re.search(r'"?\d+:(\d+)"?', text)
                    if m:
                        port = int(m.group(1))
                        if 1024 <= port <= 65535:
                            return port
                except OSError:
                    pass

        # .env files
        for env_name in (".env", ".env.example", ".env.sample"):
            env = root / env_name
            if env.is_file():
                try:
                    text = env.read_text(errors="replace")
                    m = re.search(r"PORT\s*=\s*(\d+)", text)
                    if m:
                        port = int(m.group(1))
                        if 1024 <= port <= 65535:
                            return port
                except OSError:
                    pass

        # package.json scripts (port in dev/start scripts)
        if language in ("javascript", "typescript"):
            pkg = root / "package.json"
            if pkg.is_file():
                try:
                    data = json.loads(pkg.read_text(errors="replace"))
                    scripts = data.get("scripts", {})
                    for key in ("start", "dev", "serve"):
                        cmd = scripts.get(key, "")
                        m = re.search(r"--port\s+(\d+)", cmd)
                        if not m:
                            m = re.search(r"-p\s+(\d+)", cmd)
                        if m:
                            port = int(m.group(1))
                            if 1024 <= port <= 65535:
                                return port
                except (json.JSONDecodeError, OSError):
                    pass

        # C#: launchSettings.json
        if language == "csharp":
            for launch_path in (
                root / "Properties" / "launchSettings.json",
                root / "launchSettings.json",
            ):
                if launch_path.is_file():
                    try:
                        text = launch_path.read_text(errors="replace")
                        m = re.search(r'"applicationUrl":\s*"https?://[^:]+:(\d+)"', text)
                        if m:
                            port = int(m.group(1))
                            if 1024 <= port <= 65535:
                                return port
                    except OSError:
                        pass

        # Elixir: config/config.exs
        if language == "elixir":
            config_exs = root / "config" / "config.exs"
            if config_exs.is_file():
                try:
                    text = config_exs.read_text(errors="replace")
                    m = re.search(r"http:\s*\[port:\s*(\d+)\]", text)
                    if not m:
                        m = re.search(r"port:\s*(\d+)", text)
                    if m:
                        port = int(m.group(1))
                        if 1024 <= port <= 65535:
                            return port
                except OSError:
                    pass

        # Ruby: config.ru / puma.rb
        if language == "ruby":
            for config_file in ("config.ru", "config/puma.rb"):
                fpath = root / config_file
                if fpath.is_file():
                    result = self._scan_file(fpath, language)
                    if result is not None:
                        return result

        return None

    def _scan_source_files(self, root: Path, language: str) -> int | None:
        patterns = PORT_PATTERNS.get(language, [])
        if not patterns:
            return None

        ext_map: dict[str, list[str]] = {
            "python": [".py"],
            "javascript": [".js", ".mjs"],
            "typescript": [".ts"],
            "go": [".go"],
            "ruby": [".rb"],
            "csharp": [".cs"],
            "elixir": [".ex", ".exs"],
        }
        extensions = ext_map.get(language, [])
        skip_dirs = {
            "node_modules", ".git", "__pycache__", "venv", ".venv",
            "vendor", "dist", "build", "target", ".next", ".nuxt",
        }

        for ext in extensions:
            try:
                for fpath in sorted(root.rglob(f"*{ext}")):
                    if any(part in skip_dirs for part in fpath.parts):
                        continue
                    if fpath.stat().st_size > 256_000:
                        continue
                    result = self._scan_file(fpath, language)
                    if result is not None:
                        return result
            except OSError:
                continue

        return None
