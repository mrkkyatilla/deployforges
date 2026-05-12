import time

import redis.asyncio as redis
from fastapi import HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from api.config import settings

_redis: redis.Redis | None = None


async def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


TIER_LIMITS = {
    "free": settings.rate_limit_free,
    "pro": settings.rate_limit_pro,
    "enterprise": 10_000,
}


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/v1/health"):
            return await call_next(request)

        api_key = request.headers.get("x-api-key", "")
        if not api_key:
            return await call_next(request)

        r = await get_redis()
        window = settings.rate_limit_window_seconds
        now = int(time.time())
        window_key = f"rl:{api_key}:{now // window}"

        current = await r.incr(window_key)
        if current == 1:
            await r.expire(window_key, window)

        tier = await r.get(f"tier:{api_key}") or "free"
        limit = TIER_LIMITS.get(tier, TIER_LIMITS["free"])

        if current > limit:
            raise HTTPException(
                status_code=429,
                detail={
                    "type": "https://api.deployforge.dev/errors/rate-limit-exceeded",
                    "title": "Rate Limit Exceeded",
                    "status": 429,
                    "detail": f"Rate limit of {limit} requests per hour exceeded.",
                },
                headers={
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(((now // window) + 1) * window),
                    "Retry-After": str(window - (now % window)),
                },
            )

        response: Response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limit - current))
        response.headers["X-RateLimit-Reset"] = str(((now // window) + 1) * window)
        return response
