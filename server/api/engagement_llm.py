"""Engagement LLM: comment generation endpoints for phone engagement sessions.

Called by the phone during engagement sessions via HTTP.
Supports multiple LLM providers: OpenAI, Grok (xAI), OpenRouter, Anthropic Claude.
Falls back to a random comment pool if LLM is not configured or fails.
"""
from __future__ import annotations

import logging
import random

from fastapi import APIRouter, Depends, Request

from server.config import VPSConfig
from server.dependencies import get_config
from server.llm_client import PROVIDER_DEFAULTS, generate_comment, resolve_base_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/engagement", tags=["engagement-llm"])

_COMMENT_POOL = [
    "красиво!", "вау, нравится", "огонь контент", "класс",
    "супер видео", "круто снято!", "очень красиво",
    "великолепно", "прекрасно!", "шикарно выглядит",
    "мило", "обожаю такое", "замечательно", "восхитительно",
    "атмосферно", "настроение поднял",
]


@router.post("/generate-comment")
async def generate_comment_endpoint(
    request: Request,
    config: VPSConfig = Depends(get_config),
) -> dict:
    """Generate a contextual comment for an Instagram reel.

    If LLM is configured (provider + api_key set), calls the LLM.
    Otherwise returns a random comment from a fallback pool.
    """
    body = await request.json()
    target_username = body.get("targetUsername", "")
    reel_caption = body.get("reelCaption", "")

    # Check if LLM is configured
    if config.llm_provider and config.llm_api_key:
        try:
            base_url = resolve_base_url(config.llm_provider, config.llm_base_url)
            if not base_url:
                logger.warning("LLM provider '%s' has no base_url", config.llm_provider)
                return {"comment": random.choice(_COMMENT_POOL)}

            comment = await generate_comment(
                provider=config.llm_provider,
                base_url=base_url,
                api_key=config.llm_api_key,
                model=config.llm_model,
                target_username=target_username,
                reel_caption=reel_caption,
                timeout=config.engagement_llm_timeout,
            )
            logger.info("LLM comment generated for @%s: %s", target_username, comment[:50])
            return {"comment": comment}
        except Exception as exc:
            logger.warning("LLM comment generation failed, using fallback: %s", exc)
            return {"comment": random.choice(_COMMENT_POOL)}

    # No LLM configured — fallback
    return {"comment": random.choice(_COMMENT_POOL)}


@router.post("/generate-comment-vision")
async def generate_comment_vision(
    request: Request,
    config: VPSConfig = Depends(get_config),
) -> dict:
    """Vision LLM comment generation (with screenshots). Falls back to text-only."""
    return await generate_comment_endpoint(request, config)


@router.get("/llm/providers")
async def list_providers(
    config: VPSConfig = Depends(get_config),
) -> dict:
    """Return available LLM providers with their default models.

    Used by the frontend Settings page to populate provider/model dropdowns.
    """
    providers = {}
    for name, info in PROVIDER_DEFAULTS.items():
        providers[name] = {
            "base_url": info["base_url"],
            "models": info["models"],
        }
    return {
        "providers": providers,
        "current": {
            "provider": config.llm_provider,
            "model": config.llm_model,
            "base_url": config.llm_base_url,
            "configured": bool(config.llm_provider and config.llm_api_key),
        },
    }
