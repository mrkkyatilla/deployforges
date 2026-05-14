from __future__ import annotations

import logging
from pathlib import Path

from core.ai.token_manager import estimate_tokens

logger = logging.getLogger(__name__)

CONFIG_FILES_PRIORITY = [
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "composer.json",
    "Gemfile",
    "mix.exs",
    "tsconfig.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
]

FRAMEWORK_CONFIG_PRIORITY = [
    "vite.config.js",
    "vite.config.ts",
    "next.config.js",
    "next.config.mjs",
    "next.config.ts",
    "nuxt.config.js",
    "nuxt.config.ts",
    "webpack.config.js",
    "nest-cli.json",
    "angular.json",
    ".streamlit/config.toml",
    "manage.py",
]

ENTRYPOINT_PRIORITY = [
    "main.py",
    "app.py",
    "server.py",
    "wsgi.py",
    "asgi.py",
    "index.js",
    "index.ts",
    "server.js",
    "server.ts",
    "app.js",
    "app.ts",
    "main.go",
    "cmd/main.go",
    "cmd/server/main.go",
    "src/main.rs",
    "src/main.java",
    "Program.cs",
]

OTHER_CONFIG_PRIORITY = [
    ".env.example",
    ".env.sample",
    "nginx.conf",
    "Procfile",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
]


class ContextBuilder:
    """Builds minimum-necessary context for AI prompts within token budget."""

    def build_critical_files(
        self,
        root_path: str,
        max_tokens: int = 10_000,
        *,
        aggressive: bool = False,
    ) -> dict[str, str]:
        if aggressive:
            max_tokens = max(1200, int(max_tokens * 0.68))
        root = Path(root_path)
        selected: dict[str, str] = {}
        remaining_tokens = max_tokens

        all_priorities = (
            CONFIG_FILES_PRIORITY
            + FRAMEWORK_CONFIG_PRIORITY
            + ENTRYPOINT_PRIORITY
            + OTHER_CONFIG_PRIORITY
        )

        for filename in all_priorities:
            if remaining_tokens <= 200:
                break

            filepath = root / filename
            if not filepath.is_file():
                continue

            try:
                content = filepath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            tokens = estimate_tokens(content)
            if tokens <= remaining_tokens:
                selected[filename] = content
                remaining_tokens -= tokens
            else:
                max_chars = remaining_tokens * 4
                selected[filename] = content[:max_chars] + "\n... (truncated)"
                remaining_tokens = 0

        logger.info(
            "Context built: %d files, ~%d tokens",
            len(selected),
            max_tokens - remaining_tokens,
        )
        return selected

    def build_file_tree_list(self, root_path: str, max_entries: int = 200) -> list[str]:
        root = Path(root_path)
        entries: list[str] = []

        skip_dirs = {
            "node_modules", ".git", "__pycache__", "venv", ".venv",
            "dist", "build", "target", ".next", ".nuxt", "vendor",
            "coverage", ".pytest_cache", ".mypy_cache", ".tox",
            ".gradle", ".idea", ".vscode",
        }

        for item in sorted(root.rglob("*")):
            if any(part in skip_dirs for part in item.parts):
                continue
            if len(entries) >= max_entries:
                break
            try:
                rel = item.relative_to(root)
                entries.append(str(rel))
            except ValueError:
                continue

        return entries
