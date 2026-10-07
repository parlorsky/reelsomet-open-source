"""Multi-provider LLM client for engagement comment generation.

Supports:
- OpenAI (api.openai.com) — gpt-4o, gpt-4o-mini, gpt-4.1, o4-mini
- Grok / xAI (api.x.ai) — grok-3-latest, grok-3-mini-latest
- OpenRouter (openrouter.ai) — any model via unified API
- Anthropic Claude (api.anthropic.com) — claude-sonnet-4, claude-haiku-3.5

OpenAI, Grok, and OpenRouter all use the same OpenAI-compatible format.
Anthropic uses its own Messages API format.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Provider → default base URL (user can override)
# Model lists fetched from live APIs — April 2026
PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "models": [
            "gpt-5.4", "gpt-5.4-pro", "gpt-5.4-mini", "gpt-5.4-nano",
            "gpt-5.3-codex", "gpt-5.2", "gpt-5.2-pro",
            "gpt-5", "gpt-5-mini", "gpt-5-nano",
            "gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano",
            "gpt-4o", "gpt-4o-mini",
            "o4-mini", "o3", "o3-pro", "o3-mini",
        ],
    },
    "grok": {
        "base_url": "https://api.x.ai/v1",
        "models": [
            "grok-4.20-0309-non-reasoning", "grok-4.20-0309-reasoning",
            "grok-4-1-fast-non-reasoning", "grok-4-1-fast-reasoning",
            "grok-4-fast-non-reasoning", "grok-4-fast-reasoning",
            "grok-4-0709",
            "grok-3", "grok-3-mini",
            "grok-code-fast-1",
        ],
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "models": [
            "anthropic/claude-opus-4.6", "anthropic/claude-sonnet-4.6",
            "openai/gpt-5.4", "openai/gpt-5.4-pro", "openai/gpt-5.4-mini", "openai/gpt-5.4-nano",
            "x-ai/grok-4.20", "x-ai/grok-4.20-multi-agent",
            "google/gemini-3.1-pro-preview", "google/gemini-3.1-flash-lite-preview",
            "google/gemini-2.5-pro", "google/gemini-2.5-flash",
            "deepseek/deepseek-v3.2", "deepseek/deepseek-r1-0528",
            "meta-llama/llama-4-maverick",
            "qwen/qwen3.6-plus:free",
            "meta-llama/llama-3.3-70b-instruct:free",
        ],
    },
    "anthropic": {
        "base_url": "https://api.anthropic.com",
        "models": [
            "claude-opus-4-6", "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001", "claude-haiku-4-5",
            "claude-sonnet-4-5-20250929", "claude-opus-4-5-20251101",
            "claude-sonnet-4-20250514", "claude-opus-4-20250514",
        ],
    },
}


async def generate_comment(
    provider: str,
    base_url: str,
    api_key: str,
    model: str,
    target_username: str,
    reel_caption: str,
    timeout: float = 30.0,
) -> str:
    """Generate a contextual Instagram comment via LLM.

    Returns the generated comment text, or raises on failure.
    """
    system_prompt = (
        "Ты — живой пользователь Instagram. Напиши ОДИН короткий комментарий к рилсу. "
        "Комментарий должен быть естественным, человечным, без хэштегов, без эмодзи-спама. "
        "1-2 предложения максимум. Пиши на русском. Не начинай с 'Класс!' или 'Круто!' — "
        "будь разнообразнее."
    )
    user_prompt = f"Канал: @{target_username}\nПодпись рилса: {reel_caption}\n\nНапиши комментарий:"

    if provider == "anthropic":
        return await _call_anthropic(base_url, api_key, model, system_prompt, user_prompt, timeout)
    else:
        # OpenAI-compatible: openai, grok, openrouter
        return await _call_openai_compat(base_url, api_key, model, system_prompt, user_prompt, timeout)


async def _call_openai_compat(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float,
) -> str:
    """Call an OpenAI-compatible chat completions endpoint."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 150,
        "temperature": 0.9,
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()

    choices = data.get("choices", [])
    if not choices:
        raise ValueError("Empty choices in LLM response")
    return choices[0]["message"]["content"].strip()


async def _call_anthropic(
    base_url: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    timeout: float,
) -> str:
    """Call the Anthropic Messages API (non-OpenAI format)."""
    url = f"{base_url.rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": 150,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": user_prompt},
        ],
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()

    content = data.get("content", [])
    if not content:
        raise ValueError("Empty content in Anthropic response")
    return content[0]["text"].strip()


def resolve_base_url(provider: str, custom_url: str) -> str:
    """Return the base URL for a provider, using the default if not overridden."""
    if custom_url:
        return custom_url
    defaults = PROVIDER_DEFAULTS.get(provider, {})
    return defaults.get("base_url", "")
