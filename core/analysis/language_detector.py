from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from core.analysis.fingerprint import FileTree, LanguageDetection

logger = logging.getLogger(__name__)

LANGUAGE_SIGNALS: dict[str, dict] = {
    "python": {
        "extensions": [".py", ".pyw", ".pyi"],
        "config_files": [
            "requirements.txt",
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "Pipfile",
            "tox.ini",
        ],
        "weight_config": 10,
        "weight_extension": 1,
    },
    "javascript": {
        "extensions": [".js", ".jsx", ".mjs", ".cjs"],
        "config_files": [
            "package.json",
            ".eslintrc.json",
            ".eslintrc.js",
            "babel.config.js",
            "webpack.config.js",
        ],
        "weight_config": 10,
        "weight_extension": 1,
    },
    "typescript": {
        "extensions": [".ts", ".tsx"],
        "config_files": ["tsconfig.json", "tsconfig.build.json"],
        "weight_config": 10,
        "weight_extension": 1,
    },
    "go": {
        "extensions": [".go"],
        "config_files": ["go.mod", "go.sum"],
        "weight_config": 10,
        "weight_extension": 1,
    },
    "rust": {
        "extensions": [".rs"],
        "config_files": ["Cargo.toml", "Cargo.lock"],
        "weight_config": 10,
        "weight_extension": 1,
    },
    "java": {
        "extensions": [".java"],
        "config_files": [
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
        ],
        "weight_config": 10,
        "weight_extension": 1,
    },
    "php": {
        "extensions": [".php"],
        "config_files": ["composer.json", "composer.lock", "artisan"],
        "weight_config": 10,
        "weight_extension": 1,
    },
    "ruby": {
        "extensions": [".rb", ".rake"],
        "config_files": ["Gemfile", "Gemfile.lock", "Rakefile", "config.ru"],
        "weight_config": 10,
        "weight_extension": 1,
    },
    "csharp": {
        "extensions": [".cs"],
        "config_files": [".csproj", ".sln", "Program.cs"],
        "weight_config": 10,
        "weight_extension": 1,
    },
    "elixir": {
        "extensions": [".ex", ".exs"],
        "config_files": ["mix.exs", "mix.lock"],
        "weight_config": 10,
        "weight_extension": 1,
    },
}

_SCORE_THRESHOLD = 2.0


class LanguageDetector:

    def detect(self, file_tree: FileTree) -> LanguageDetection:
        scores: dict[str, float] = {}
        file_names = {Path(n.path).name for n in file_tree.nodes}

        for lang, signals in LANGUAGE_SIGNALS.items():
            score = 0.0
            for cfg in signals["config_files"]:
                if cfg in file_names:
                    score += signals["weight_config"]
            for node in file_tree.nodes:
                if node.extension in signals["extensions"]:
                    score += signals["weight_extension"]
            if score > 0:
                scores[lang] = score

        if not scores:
            return LanguageDetection(
                primary="unknown", version=None, secondary=[], scores={}
            )

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        primary = ranked[0][0]

        # TypeScript projects always include JS files; promote TS if both present
        if primary == "javascript" and "typescript" in scores:
            if scores["typescript"] >= scores["javascript"] * 0.5:
                primary = "typescript"

        secondary = [
            lang
            for lang, sc in ranked
            if lang != primary and sc >= _SCORE_THRESHOLD
        ]

        version = self._detect_version(file_tree.root_path, primary)

        return LanguageDetection(
            primary=primary,
            version=version,
            secondary=secondary,
            scores=scores,
        )

    # ------------------------------------------------------------------
    # Version detection helpers
    # ------------------------------------------------------------------

    def _detect_version(self, root_path: str, language: str) -> str | None:
        handlers: dict[str, callable] = {
            "python": self._python_version,
            "javascript": self._node_version,
            "typescript": self._node_version,
            "go": self._go_version,
            "rust": self._rust_version,
            "java": self._java_version,
        }
        handler = handlers.get(language)
        if handler is None:
            return None
        try:
            return handler(Path(root_path))
        except Exception:
            logger.debug("Version detection failed for %s", language, exc_info=True)
            return None

    @staticmethod
    def _python_version(root: Path) -> str | None:
        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            text = pyproject.read_text(errors="replace")
            m = re.search(r'requires-python\s*=\s*"([^"]+)"', text)
            if m:
                return m.group(1).strip()

        runtime = root / "runtime.txt"
        if runtime.is_file():
            text = runtime.read_text(errors="replace").strip()
            m = re.search(r"python-([\d.]+)", text, re.IGNORECASE)
            if m:
                return m.group(1)

        return None

    @staticmethod
    def _node_version(root: Path) -> str | None:
        pkg = root / "package.json"
        if not pkg.is_file():
            return None
        try:
            data = json.loads(pkg.read_text(errors="replace"))
            return data.get("engines", {}).get("node")
        except (json.JSONDecodeError, KeyError):
            return None

    @staticmethod
    def _go_version(root: Path) -> str | None:
        gomod = root / "go.mod"
        if not gomod.is_file():
            return None
        for line in gomod.read_text(errors="replace").splitlines():
            stripped = line.strip()
            if stripped.startswith("go "):
                return stripped.split(maxsplit=1)[1]
        return None

    @staticmethod
    def _rust_version(root: Path) -> str | None:
        cargo = root / "Cargo.toml"
        if not cargo.is_file():
            return None
        text = cargo.read_text(errors="replace")
        m = re.search(r'edition\s*=\s*"(\d{4})"', text)
        return m.group(1) if m else None

    @staticmethod
    def _java_version(root: Path) -> str | None:
        pom = root / "pom.xml"
        if pom.is_file():
            text = pom.read_text(errors="replace")
            m = re.search(r"<java\.version>([\d.]+)</java\.version>", text)
            if m:
                return m.group(1)
            m = re.search(r"<maven\.compiler\.source>([\d.]+)</maven\.compiler\.source>", text)
            if m:
                return m.group(1)

        for name in ("build.gradle", "build.gradle.kts"):
            gradle = root / name
            if gradle.is_file():
                text = gradle.read_text(errors="replace")
                m = re.search(r"sourceCompatibility\s*=\s*['\"]?([\d.]+)", text)
                if m:
                    return m.group(1)

        return None
