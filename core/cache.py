from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import redis.asyncio as redis

from api.config import settings

logger = logging.getLogger(__name__)

_SIGNIFICANT_FIELDS = (
    "language",
    "language_version",
    "framework",
    "framework_version",
    "has_native_deps",
    "port",
    "is_static",
)


@dataclass
class CacheResult:
    dockerfile: str
    dockerignore: str
    cache_key: str
    cached_at: float


@dataclass
class CacheStats:
    hits: int
    misses: int
    hit_rate: float
    total_cached: int


class DockerfileCache:
    """Cache generated Dockerfiles by project fingerprint signature."""

    CACHE_TTL = 86400 * 7  # 7 days
    KEY_PREFIX = "df_cache:"

    def __init__(self) -> None:
        self._redis: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis:
        if self._redis is None:
            self._redis = redis.from_url(
                settings.redis_url,
                decode_responses=True,
            )
        return self._redis

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    @staticmethod
    def _compute_cache_key(fingerprint: dict[str, Any]) -> str:
        deps = fingerprint.get("dependencies", fingerprint.get("dep_names", []))
        if isinstance(deps, list):
            sorted_deps = sorted(str(d) for d in deps)
        else:
            sorted_deps = []

        significant = {k: fingerprint.get(k) for k in _SIGNIFICANT_FIELDS}
        significant["dep_names"] = sorted_deps
        canonical = json.dumps(significant, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    async def get(self, fingerprint: dict[str, Any]) -> CacheResult | None:
        r = await self._get_redis()
        cache_key = self._compute_cache_key(fingerprint)
        full_key = f"{self.KEY_PREFIX}{cache_key}"

        try:
            raw = await r.get(full_key)
        except Exception:
            logger.exception("Redis GET failed for key %s", full_key)
            return None

        if raw is None:
            await self._incr_misses(r)
            logger.debug("Cache miss for key %s", cache_key)
            return None

        await self._incr_hits(r)
        logger.info("Cache hit for key %s", cache_key)

        data = json.loads(raw)
        return CacheResult(
            dockerfile=data["dockerfile"],
            dockerignore=data.get("dockerignore", ""),
            cache_key=cache_key,
            cached_at=data.get("cached_at", 0.0),
        )

    async def set(
        self,
        fingerprint: dict[str, Any],
        dockerfile: str,
        dockerignore: str,
    ) -> str:
        r = await self._get_redis()
        cache_key = self._compute_cache_key(fingerprint)
        full_key = f"{self.KEY_PREFIX}{cache_key}"

        payload = json.dumps({
            "dockerfile": dockerfile,
            "dockerignore": dockerignore,
            "cached_at": time.time(),
        })

        try:
            await r.set(full_key, payload, ex=self.CACHE_TTL)
            logger.info("Cached Dockerfile under key %s (ttl=%ds)", cache_key, self.CACHE_TTL)
        except Exception:
            logger.exception("Redis SET failed for key %s", full_key)

        return cache_key

    async def invalidate(self, fingerprint: dict[str, Any]) -> None:
        r = await self._get_redis()
        cache_key = self._compute_cache_key(fingerprint)
        full_key = f"{self.KEY_PREFIX}{cache_key}"

        try:
            await r.delete(full_key)
            logger.info("Invalidated cache key %s", cache_key)
        except Exception:
            logger.exception("Redis DELETE failed for key %s", full_key)

    async def get_stats(self) -> CacheStats:
        r = await self._get_redis()

        try:
            hits = int(await r.get(f"{self.KEY_PREFIX}hits") or 0)
            misses = int(await r.get(f"{self.KEY_PREFIX}misses") or 0)
        except Exception:
            logger.exception("Redis stats read failed")
            hits, misses = 0, 0

        total = hits + misses
        hit_rate = (hits / total * 100) if total > 0 else 0.0

        total_cached = 0
        try:
            cursor: int | bytes = 0
            while True:
                cursor, keys = await r.scan(
                    cursor=cursor,
                    match=f"{self.KEY_PREFIX}*",
                    count=500,
                )
                total_cached += sum(
                    1 for k in keys
                    if k not in (f"{self.KEY_PREFIX}hits", f"{self.KEY_PREFIX}misses")
                )
                if cursor == 0:
                    break
        except Exception:
            logger.exception("Redis SCAN failed during stats collection")

        return CacheStats(
            hits=hits,
            misses=misses,
            hit_rate=round(hit_rate, 2),
            total_cached=total_cached,
        )

    @staticmethod
    async def _incr_hits(r: redis.Redis) -> None:
        try:
            await r.incr("df_cache:hits")
        except Exception:
            logger.debug("Failed to increment cache hit counter")

    @staticmethod
    async def _incr_misses(r: redis.Redis) -> None:
        try:
            await r.incr("df_cache:misses")
        except Exception:
            logger.debug("Failed to increment cache miss counter")
