"""Redis-backed sliding-window rate limiter."""
import time

from fastapi import HTTPException

from app.config import settings
from app.storage import get_redis


WINDOW_SECONDS = 60


def check_rate_limit(user_id: str) -> dict:
    now = time.time()
    key = f"rate:{user_id}"
    redis_client = get_redis()

    pipe = redis_client.pipeline()
    pipe.zremrangebyscore(key, 0, now - WINDOW_SECONDS)
    pipe.zcard(key)
    pipe.execute()

    request_count = redis_client.zcard(key)
    if request_count >= settings.rate_limit_per_minute:
        oldest = redis_client.zrange(key, 0, 0, withscores=True)
        retry_after = WINDOW_SECONDS
        if oldest:
            retry_after = max(1, int(oldest[0][1] + WINDOW_SECONDS - now) + 1)
        raise HTTPException(
            status_code=429,
            detail={
                "error": "Rate limit exceeded",
                "limit": settings.rate_limit_per_minute,
                "window_seconds": WINDOW_SECONDS,
                "retry_after_seconds": retry_after,
            },
            headers={
                "Retry-After": str(retry_after),
                "X-RateLimit-Limit": str(settings.rate_limit_per_minute),
                "X-RateLimit-Remaining": "0",
            },
        )

    member = f"{now}:{request_count}"
    pipe = redis_client.pipeline()
    pipe.zadd(key, {member: now})
    pipe.expire(key, WINDOW_SECONDS * 2)
    pipe.execute()

    remaining = settings.rate_limit_per_minute - request_count - 1
    return {
        "limit": settings.rate_limit_per_minute,
        "remaining": max(0, remaining),
        "window_seconds": WINDOW_SECONDS,
    }
