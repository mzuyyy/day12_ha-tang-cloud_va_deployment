"""Redis-backed monthly cost guard."""
from datetime import datetime, timezone

from fastapi import HTTPException

from app.config import settings
from app.storage import get_redis


def check_budget(user_id: str, estimated_cost_usd: float = 0.0) -> None:
    current = _current_spend(user_id)
    if current + estimated_cost_usd > settings.monthly_budget_usd:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "Monthly budget exceeded",
                "used_usd": round(current, 6),
                "estimated_cost_usd": round(estimated_cost_usd, 6),
                "budget_usd": settings.monthly_budget_usd,
                "resets_at": "start of next UTC month",
            },
        )


def record_usage(user_id: str, input_tokens: int, output_tokens: int) -> dict:
    cost = (
        (input_tokens / 1000) * settings.price_per_1k_input_tokens
        + (output_tokens / 1000) * settings.price_per_1k_output_tokens
    )
    redis_client = get_redis()
    key = _budget_key(user_id)
    pipe = redis_client.pipeline()
    pipe.incrbyfloat(key, cost)
    pipe.expire(key, 32 * 24 * 3600)
    pipe.hincrby(_usage_key(user_id), "requests", 1)
    pipe.hincrby(_usage_key(user_id), "input_tokens", input_tokens)
    pipe.hincrby(_usage_key(user_id), "output_tokens", output_tokens)
    pipe.expire(_usage_key(user_id), 32 * 24 * 3600)
    pipe.execute()
    return get_usage(user_id)


def get_usage(user_id: str) -> dict:
    redis_client = get_redis()
    usage = redis_client.hgetall(_usage_key(user_id))
    spent = _current_spend(user_id)
    return {
        "user_id": user_id,
        "month": _month_key(),
        "requests": int(usage.get("requests", 0)),
        "input_tokens": int(usage.get("input_tokens", 0)),
        "output_tokens": int(usage.get("output_tokens", 0)),
        "cost_usd": round(spent, 6),
        "budget_usd": settings.monthly_budget_usd,
        "budget_remaining_usd": round(max(0.0, settings.monthly_budget_usd - spent), 6),
        "budget_used_pct": round((spent / settings.monthly_budget_usd) * 100, 2)
        if settings.monthly_budget_usd else 100.0,
    }


def _current_spend(user_id: str) -> float:
    value = get_redis().get(_budget_key(user_id))
    return float(value or 0)


def _budget_key(user_id: str) -> str:
    return f"budget:{user_id}:{_month_key()}"


def _usage_key(user_id: str) -> str:
    return f"usage:{user_id}:{_month_key()}"


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")
