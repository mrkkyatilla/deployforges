"""Redis-backed cache for expensive AI sub-steps (e.g. Dockerfile plan JSON)."""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from api.config import settings

logger = logging.getLogger(__name__)


def fingerprint_cache_digest(fingerprint: dict) -> str:
    subset = {
        "language": fingerprint.get("language"),
        "dependencies": fingerprint.get("dependencies"),
        "framework": fingerprint.get("framework"),
        "is_monorepo": fingerprint.get("is_monorepo"),
        "port": fingerprint.get("port"),
        "entrypoint": fingerprint.get("entrypoint"),
    }
    blob = json.dumps(subset, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def enriched_fingerprint_cache_digest(enriched_fp: dict) -> str:
    base = fingerprint_cache_digest(enriched_fp)
    cf = enriched_fp.get("critical_files") or ""
    cf_h = hashlib.sha256(str(cf).encode()).hexdigest()[:40]
    return f"{base}:{cf_h}"


async def cache_get_json(key: str) -> Any | None:
    if not (settings.ai_pipeline_cache_ttl_seconds or 0):
        return None
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            raw = await r.get(key)
        finally:
            await r.aclose()
        if not raw:
            return None
        return json.loads(raw)
    except Exception:
        logger.debug("pipeline_cache get failed", exc_info=True)
        return None


async def cache_set_json(key: str, value: Any) -> None:
    ttl = int(settings.ai_pipeline_cache_ttl_seconds or 0)
    if ttl <= 0:
        return
    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            await r.set(key, json.dumps(value, default=str), ex=ttl)
        finally:
            await r.aclose()
    except Exception:
        logger.debug("pipeline_cache set failed", exc_info=True)


def plan_cache_key(fingerprint_digest: str) -> str:
    return f"df:dockerfile_plan:{fingerprint_digest}"


def metadata_cache_key(fingerprint_digest: str) -> str:
    return f"df:dockerfile_metadata:{fingerprint_digest}"
