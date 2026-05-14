from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from api.config import settings

_MAX_FILE_SIZE = 1_048_576  # 1 MB
_SKIP_DIRS = {".git", "node_modules", "__pycache__"}
# Path segments: skip heuristic secret / .env / suspicious scans (tutorial & test false positives).
_SKIP_TREE_DIR_NAMES = frozenset({
    "tests",
    "test",
    "testing",
    "__tests__",
    "examples",
    "example",
    "docs",
    "doc",
    "documentation",
    "fixtures",
    "fixture",
    "testdata",
    "test_data",
    "__snapshots__",
    "site",
    "_site",
})
_DANGEROUS_FILE_PATTERNS = {
    "id_rsa",
    "id_ecdsa",
    "id_ed25519",
}
_DANGEROUS_EXTENSIONS = {".pem", ".key"}

_SUSPICIOUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("fork_bomb", re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")),
    ("crypto_miner", re.compile(r"(?i)(xmrig|minergate|coinhive|cryptonight)")),
    ("destructive_rm", re.compile(r"rm\s+-rf\s+/")),
    ("curl_pipe_sh", re.compile(r"curl\s+[^\|]+\|\s*(?:ba)?sh")),
    ("wget_pipe_sh", re.compile(r"wget\s+[^\|]+\|\s*(?:ba)?sh")),
]


def _is_skipped_scan_path(rel: str) -> bool:
    """True if file lives under tutorial / test trees where fake credentials are expected."""
    parts = Path(rel).parts
    lowered = {p.lower() for p in parts}
    if lowered & _SKIP_TREE_DIR_NAMES:
        return True
    for p in parts:
        pl = p.lower()
        if pl.startswith("tests_") or pl.startswith("test_"):
            return True
    return False


@dataclass
class ScanResult:
    is_safe: bool
    secrets_found: list[dict[str, str | int]] = field(default_factory=list)
    dangerous_files: list[str] = field(default_factory=list)
    suspicious_scripts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class SecurityScanner:
    def __init__(self) -> None:
        self._secret_patterns = [
            re.compile(p) for p in settings.secret_patterns
        ]

    async def scan(self, project_path: Path) -> ScanResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, partial(self._scan_sync, project_path)
        )

    def _scan_sync(self, project_path: Path) -> ScanResult:
        secrets: list[dict[str, str | int]] = []
        dangerous: list[str] = []
        suspicious: list[str] = []
        warnings: list[str] = []

        for path in project_path.rglob("*"):
            if not path.is_file():
                continue
            if any(skip in path.parts for skip in _SKIP_DIRS):
                continue

            rel = str(path.relative_to(project_path))

            if _is_skipped_scan_path(rel):
                continue

            if self._is_dangerous_file(path):
                dangerous.append(rel)
                continue

            if self._is_env_with_values(path):
                dangerous.append(rel)
                continue

            if path.stat().st_size > _MAX_FILE_SIZE:
                warnings.append(f"Skipped large file: {rel} ({path.stat().st_size} bytes)")
                continue

            if self._is_binary(path):
                continue

            try:
                content = path.read_text(errors="replace")
            except OSError:
                warnings.append(f"Could not read: {rel}")
                continue

            for line_num, line in enumerate(content.splitlines(), start=1):
                for pattern in self._secret_patterns:
                    if pattern.search(line):
                        secrets.append({
                            "file": rel,
                            "line": line_num,
                            "pattern": pattern.pattern,
                        })

            for name, pat in _SUSPICIOUS_PATTERNS:
                if pat.search(content):
                    suspicious.append(f"{rel}: {name}")

        is_safe = not secrets and not dangerous and not suspicious
        return ScanResult(
            is_safe=is_safe,
            secrets_found=secrets,
            dangerous_files=dangerous,
            suspicious_scripts=suspicious,
            warnings=warnings,
        )

    @staticmethod
    def _is_dangerous_file(path: Path) -> bool:
        if path.name in _DANGEROUS_FILE_PATTERNS:
            return True
        if path.suffix in _DANGEROUS_EXTENSIONS:
            try:
                header = path.read_bytes()[:100].decode(errors="replace")
            except OSError:
                return False
            if "PRIVATE KEY" in header:
                return True
        return False

    @staticmethod
    def _is_env_with_values(path: Path) -> bool:
        if path.name != ".env" and not path.name.startswith(".env."):
            return False
        try:
            for line in path.read_text(errors="replace").splitlines()[:50]:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    _, _, value = stripped.partition("=")
                    if value.strip().strip("'\""):
                        return True
        except OSError:
            pass
        return False

    @staticmethod
    def _is_binary(path: Path) -> bool:
        try:
            chunk = path.read_bytes()[:512]
            return b"\x00" in chunk
        except OSError:
            return True
