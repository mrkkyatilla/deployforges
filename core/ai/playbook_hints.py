"""Redis-backed playbook hints (curated strings; no embeddings)."""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from api.config import settings

logger = logging.getLogger(__name__)

_DATA_PATH = Path(__file__).resolve().parent / "data" / "playbook_hints.yaml"


@lru_cache(maxsize=1)
def _load_playbook_yaml() -> dict[str, Any]:
    if not _DATA_PATH.is_file():
        return {}
    try:
        return yaml.safe_load(_DATA_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("Failed to load playbook_hints.yaml", exc_info=True)
        return {}


def _fingerprint_matches_when(fp: dict, when: dict[str, Any]) -> bool:
    if not when:
        return True
    lang = (fp.get("language") or {}).get("primary")
    if "language_primary" in when and lang != when["language_primary"]:
        return False
    mgr = str((fp.get("dependencies") or {}).get("manager") or "").lower()
    if "dependencies_manager_in" in when:
        allowed = {str(x).lower() for x in when["dependencies_manager_in"]}
        if mgr not in allowed:
            return False
    return True


def static_hints_for_fingerprint(fingerprint: dict | None) -> list[str]:
    fp = fingerprint or {}
    data = _load_playbook_yaml()
    out: list[str] = []
    for block in data.get("static_sets") or []:
        if not isinstance(block, dict):
            continue
        when = block.get("when") or {}
        if not _fingerprint_matches_when(fp, when):
            continue
        for h in block.get("hints") or []:
            if isinstance(h, str) and h.strip():
                out.append(h.strip())
    return out[:5]


def playbook_redis_key(lang: str, fw: str, error_name: str) -> str:
    data = _load_playbook_yaml()
    prefix = str(data.get("redis_prefix") or "df:playbook:v1")
    fw_n = (fw or "unknown").lower().replace(" ", "_")[:48]
    en = (error_name or "unknown").lower().replace(" ", "_")[:64]
    return f"{prefix}:{lang}:{fw_n}:{en}"


def error_hint_template(error_name: str) -> str | None:
    block = (_load_playbook_yaml().get("error_hints") or {}).get(error_name)
    if isinstance(block, dict):
        t = block.get("text")
        return str(t).strip() if t else None
    return None


async def collect_playbook_hints_for_prompt(fingerprint: dict | None) -> list[str]:
    """Merge static YAML hints + optional Redis values (bootstrap error names)."""
    if not getattr(settings, "ai_playbook_hints_enabled", True):
        return []
    hints = static_hints_for_fingerprint(fingerprint)
    fp = fingerprint or {}
    lang = str((fp.get("language") or {}).get("primary") or "unknown").lower()
    fw = str((fp.get("framework") or {}).get("name") or "unknown")

    ttl = int(getattr(settings, "ai_playbook_hint_ttl_seconds", 0) or 0)
    if ttl <= 0:
        return hints[:5]

    keys: list[str] = []
    for name in sorted((_load_playbook_yaml().get("redis_bootstrap_error_names") or [])):
        if isinstance(name, str) and name.strip():
            keys.append(playbook_redis_key(lang, fw, name.strip()))

    if not keys:
        return hints[:5]

    try:
        import redis.asyncio as aioredis

        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            vals = await r.mget(keys)
        finally:
            await r.aclose()
        for v in vals or []:
            if not v:
                continue
            try:
                obj = json.loads(v)
                t = obj.get("text") if isinstance(obj, dict) else None
                if isinstance(t, str) and t.strip() and t not in hints:
                    hints.append(t.strip())
            except json.JSONDecodeError:
                if v.strip() and v not in hints:
                    hints.append(v.strip())
    except Exception:
        logger.debug("playbook Redis mget failed", exc_info=True)

    return hints[:5]


async def record_playbook_hints_on_success(
    fingerprint: dict | None,
    error_history: list[dict[str, Any]],
) -> None:
    """After a successful deploy, reinforce Redis hints for error names seen earlier."""
    if not getattr(settings, "ai_playbook_hints_enabled", True):
        return
    ttl = int(getattr(settings, "ai_playbook_hint_ttl_seconds", 0) or 0)
    if ttl <= 0:
        return

    fp = fingerprint or {}
    lang = str((fp.get("language") or {}).get("primary") or "unknown").lower()
    fw = str((fp.get("framework") or {}).get("name") or "unknown")

    seen: set[str] = set()
    for item in error_history or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        text = error_hint_template(name)
        if not text:
            continue
        key = playbook_redis_key(lang, fw, name)
        payload = json.dumps({"text": text, "name": name}, default=str)
        try:
            import redis.asyncio as aioredis

            r = aioredis.from_url(settings.redis_url, decode_responses=True)
            try:
                await r.set(key, payload, ex=ttl)
            finally:
                await r.aclose()
        except Exception:
            logger.debug("playbook Redis set failed key=%s", key, exc_info=True)
