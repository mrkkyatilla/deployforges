from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from uuid import UUID

import redis.asyncio as redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from core.intake.security_scan import SecurityScanner
from db.models import Project
from db.session import async_session_factory

logger = logging.getLogger(__name__)

_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".tox", ".venv", "venv"}

_CRYPTO_MINING_PATTERNS = re.compile(
    r"(?i)\b(xmrig|cpuminer|ethminer|minergate|coinhive|cryptonight|nicehash|stratum\+tcp)\b"
)
_DDOS_TOOL_PATTERNS = re.compile(
    r"(?i)\b(slowloris|hping3?|loic|hoic|goldeneye|xerxes)\b"
)
_MALWARE_SIGNATURES = re.compile(
    r"(?i)\b(metasploit|msfvenom|reverse.?shell|bind.?shell|web.?shell|c99shell|r57shell)\b"
)


@dataclass
class AbuseCheckResult:
    allowed: bool
    reason: str | None = None
    retry_after: int | None = None
    severity: str = "info"


class AbuseProtection:
    """Protects the system from abuse: rate limiting per user, content checks, budget guards."""

    MAX_TOKENS_PER_PROJECT = 50_000
    MAX_FILE_SIZE_MB = 500
    MAX_FILES_COUNT = 10_000
    MAX_AI_CALLS_PER_PROJECT = 10

    MAX_PROJECTS_PER_HOUR: dict[str, int] = {
        "free": 2,
        "starter": 5,
        "builder": 20,
        "scale": 100,
    }

    MAX_CONCURRENT_PROJECTS: dict[str, int] = {
        "free": 1,
        "starter": 2,
        "builder": 5,
        "scale": 20,
    }

    _RATE_KEY_PREFIX = "abuse:rate:"
    _CONCURRENT_KEY_PREFIX = "abuse:concurrent:"
    _AI_CALLS_KEY_PREFIX = "abuse:ai_calls:"

    def __init__(self) -> None:
        self._redis: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(settings.redis_url, decode_responses=True)
        return self._redis

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def check_can_create_project(
        self, user_id: UUID, tier: str
    ) -> AbuseCheckResult:
        r = await self._get_redis()

        hourly_limit = self.MAX_PROJECTS_PER_HOUR.get(tier, 2)
        rate_key = f"{self._RATE_KEY_PREFIX}{user_id}"

        try:
            current_count = int(await r.get(rate_key) or 0)
        except Exception:
            logger.exception("Redis read failed for rate limit check")
            return AbuseCheckResult(allowed=True)

        if current_count >= hourly_limit:
            ttl = await r.ttl(rate_key)
            retry_after = max(ttl, 60)
            logger.warning(
                "Rate limit hit for user %s (tier=%s, count=%d/%d)",
                user_id, tier, current_count, hourly_limit,
            )
            return AbuseCheckResult(
                allowed=False,
                reason=f"Rate limit exceeded: {hourly_limit} projects per hour for {tier} tier",
                retry_after=retry_after,
                severity="block",
            )

        concurrent_limit = self.MAX_CONCURRENT_PROJECTS.get(tier, 1)
        try:
            async with async_session_factory() as db:
                result = await db.execute(
                    select(func.count(Project.id)).where(
                        Project.user_id == user_id,
                        Project.status.in_(["queued", "cloning", "analyzing", "building", "processing"]),
                    )
                )
                active_count = result.scalar() or 0
        except Exception:
            logger.exception("DB query failed for concurrent project check")
            return AbuseCheckResult(allowed=True)

        if active_count >= concurrent_limit:
            logger.warning(
                "Concurrent limit hit for user %s (tier=%s, active=%d/%d)",
                user_id, tier, active_count, concurrent_limit,
            )
            return AbuseCheckResult(
                allowed=False,
                reason=f"Concurrent project limit reached: {concurrent_limit} for {tier} tier",
                retry_after=30,
                severity="block",
            )

        try:
            pipe = r.pipeline()
            pipe.incr(rate_key)
            pipe.expire(rate_key, 3600, nx=True)
            await pipe.execute()
        except Exception:
            logger.exception("Redis write failed for rate limit increment")

        return AbuseCheckResult(allowed=True)

    async def check_project_size(self, workspace_path: Path) -> AbuseCheckResult:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, partial(self._check_project_size_sync, workspace_path)
        )

    def _check_project_size_sync(self, workspace_path: Path) -> AbuseCheckResult:
        total_size = 0
        file_count = 0
        max_bytes = self.MAX_FILE_SIZE_MB * 1024 * 1024

        try:
            for path in workspace_path.rglob("*"):
                if not path.is_file():
                    continue
                if any(skip in path.parts for skip in _SKIP_DIRS):
                    continue
                file_count += 1
                total_size += path.stat().st_size

                if total_size > max_bytes:
                    return AbuseCheckResult(
                        allowed=False,
                        reason=f"Project exceeds maximum size of {self.MAX_FILE_SIZE_MB} MB",
                        severity="block",
                    )
                if file_count > self.MAX_FILES_COUNT:
                    return AbuseCheckResult(
                        allowed=False,
                        reason=f"Project exceeds maximum file count of {self.MAX_FILES_COUNT}",
                        severity="block",
                    )
        except OSError as exc:
            logger.warning("Error scanning workspace %s: %s", workspace_path, exc)

        return AbuseCheckResult(allowed=True)

    async def check_content_safety(self, workspace_path: Path) -> AbuseCheckResult:
        scanner = SecurityScanner()
        scan_result = await scanner.scan(workspace_path)

        if not scan_result.is_safe:
            reasons = []
            if scan_result.secrets_found:
                reasons.append(f"{len(scan_result.secrets_found)} secret(s) detected")
            if scan_result.dangerous_files:
                reasons.append(f"{len(scan_result.dangerous_files)} dangerous file(s)")
            if scan_result.suspicious_scripts:
                reasons.append(f"{len(scan_result.suspicious_scripts)} suspicious pattern(s)")
            return AbuseCheckResult(
                allowed=False,
                reason=f"Content safety check failed: {'; '.join(reasons)}",
                severity="block",
            )

        loop = asyncio.get_running_loop()
        extra = await loop.run_in_executor(
            None, partial(self._deep_content_check_sync, workspace_path)
        )
        if not extra.allowed:
            return extra

        return AbuseCheckResult(allowed=True)

    def _deep_content_check_sync(self, workspace_path: Path) -> AbuseCheckResult:
        total_files = 0
        binary_files = 0

        for path in workspace_path.rglob("*"):
            if not path.is_file():
                continue
            if any(skip in path.parts for skip in _SKIP_DIRS):
                continue
            total_files += 1

            if self._is_binary(path):
                binary_files += 1
                continue

            if path.stat().st_size > 2 * 1024 * 1024:
                continue

            try:
                content = path.read_text(errors="replace")
            except OSError:
                continue

            for pattern, label in [
                (_CRYPTO_MINING_PATTERNS, "crypto mining tool"),
                (_DDOS_TOOL_PATTERNS, "DDoS tool"),
                (_MALWARE_SIGNATURES, "malware signature"),
            ]:
                match = pattern.search(content)
                if match:
                    rel = str(path.relative_to(workspace_path))
                    logger.warning(
                        "Abuse content detected in %s: %s (%s)",
                        rel, match.group(), label,
                    )
                    return AbuseCheckResult(
                        allowed=False,
                        reason=f"Prohibited content detected: {label} in {rel}",
                        severity="block",
                    )

        if total_files > 0 and binary_files / total_files > 0.5:
            return AbuseCheckResult(
                allowed=False,
                reason="Excessive binary content: over 50% of files are binary",
                severity="warning",
            )

        return AbuseCheckResult(allowed=True)

    async def track_ai_calls(self, project_id: UUID) -> bool:
        r = await self._get_redis()
        key = f"{self._AI_CALLS_KEY_PREFIX}{project_id}"

        try:
            current = await r.incr(key)
            if current == 1:
                await r.expire(key, 3600)
        except Exception:
            logger.exception("Redis failed for AI call tracking on project %s", project_id)
            return True

        if current > self.MAX_AI_CALLS_PER_PROJECT:
            logger.warning(
                "AI call limit exceeded for project %s (%d/%d)",
                project_id, current, self.MAX_AI_CALLS_PER_PROJECT,
            )
            return False

        return True

    async def report_abuse(self, user_id: UUID, reason: str) -> None:
        logger.critical("ABUSE REPORT — user=%s reason=%s", user_id, reason)
        r = await self._get_redis()
        try:
            import time

            await r.lpush(
                f"abuse:reports:{user_id}",
                f"{time.time()}|{reason}",
            )
            await r.expire(f"abuse:reports:{user_id}", 86400 * 30)
        except Exception:
            logger.exception("Failed to persist abuse report for user %s", user_id)

    @staticmethod
    def _is_binary(path: Path) -> bool:
        try:
            chunk = path.read_bytes()[:512]
            return b"\x00" in chunk
        except OSError:
            return True
