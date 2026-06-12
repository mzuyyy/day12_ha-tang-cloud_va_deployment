"""LLM client with mock fallback and NVIDIA/OpenAI-compatible support."""
import json
import urllib.error
import urllib.request

from app.config import settings
from utils.mock_llm import ask as mock_ask


def ask(question: str) -> str:
    if _should_use_mock():
        return mock_ask(question)
    return _ask_openai_compatible(question)


def get_llm_mode() -> str:
    if _should_use_mock():
        return "mock"
    return f"{_provider()}:{settings.llm_model}"


def _should_use_mock() -> bool:
    provider = settings.llm_provider.lower()
    if provider == "mock":
        return True
    return not _api_key()


def _provider() -> str:
    provider = settings.llm_provider.lower()
    if provider != "auto":
        return provider
    if settings.nvidia_api_key or settings.openai_api_key.startswith("nvapi-"):
        return "nvidia"
    return "openai-compatible"


def _api_key() -> str:
    return settings.nvidia_api_key or settings.openai_api_key


def _ask_openai_compatible(question: str) -> str:
    body = {
        "model": settings.llm_model,
        "messages": [
            {
                "role": "system",
                "content": "You are a concise deployment lab assistant.",
            },
            {
                "role": "user",
                "content": question,
            },
        ],
        "temperature": 0.2,
        "max_tokens": 512,
    }
    request = urllib.request.Request(
        url=f"{settings.llm_base_url.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM API returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"LLM API request failed: {exc.reason}") from exc

    try:
        return payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected LLM API response: {payload}") from exc
