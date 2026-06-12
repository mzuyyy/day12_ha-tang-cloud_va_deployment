"""Redis storage helpers for sessions and operational state."""
import json
from datetime import datetime, timezone

import redis

from app.config import settings


class RedisUnavailable(RuntimeError):
    pass


_redis_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        _redis_client.ping()
    except redis.RedisError as exc:
        raise RedisUnavailable(str(exc)) from exc
    return _redis_client


def ping_redis() -> bool:
    return bool(get_redis().ping())


def append_message(session_id: str, role: str, content: str, user_id: str) -> list[dict]:
    redis_client = get_redis()
    message = {
        "role": role,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    key = _session_key(user_id, session_id)
    redis_client.rpush(key, json.dumps(message))
    redis_client.ltrim(key, -settings.max_history_messages, -1)
    redis_client.expire(key, settings.session_ttl_seconds)
    return load_history(session_id, user_id=user_id)


def load_history(session_id: str, user_id: str) -> list[dict]:
    redis_client = get_redis()
    messages = redis_client.lrange(_session_key(user_id, session_id), 0, -1)
    return [json.loads(message) for message in messages]


def delete_session(session_id: str, user_id: str) -> None:
    get_redis().delete(_session_key(user_id, session_id))


def _session_key(user_id: str, session_id: str) -> str:
    return f"session:{user_id}:{session_id}"
