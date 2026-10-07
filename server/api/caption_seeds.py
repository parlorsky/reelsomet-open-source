"""Caption Seeds API: CRUD for carousel caption generation prompts."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from server.config import VPSConfig
from server.dependencies import get_config, get_db_session, require_auth
from server.models import CaptionSeed

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/caption-seeds", tags=["caption-seeds"])


class SeedCreate(BaseModel):
    category: str
    seed_prompt: str
    language: str = "en"
    model: str | None = None


class SeedUpdate(BaseModel):
    category: str | None = None
    seed_prompt: str | None = None
    language: str | None = None
    is_active: bool | None = None
    model: str | None = None


@router.get("")
async def list_seeds(
    category: str | None = None,
    model: str | None = None,
    session: AsyncSession = Depends(get_db_session),
    _=Depends(require_auth),
):
    query = select(CaptionSeed).where(CaptionSeed.is_active.is_(True))
    if category:
        query = query.where(CaptionSeed.category == category)
    if model is not None:
        # Empty string ⇒ "no model" (global seeds). Non-empty ⇒ exact match.
        if model == "":
            query = query.where(CaptionSeed.model.is_(None))
        else:
            query = query.where(CaptionSeed.model == model)
    result = await session.execute(query.order_by(CaptionSeed.category, CaptionSeed.id))
    return [
        {
            "id": s.id,
            "model": s.model,
            "category": s.category,
            "seed_prompt": s.seed_prompt,
            "language": s.language,
            "is_active": s.is_active,
        }
        for s in result.scalars().all()
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_seed(
    body: SeedCreate,
    session: AsyncSession = Depends(get_db_session),
    _=Depends(require_auth),
):
    seed = CaptionSeed(
        model=body.model or None,
        category=body.category,
        seed_prompt=body.seed_prompt,
        language=body.language,
    )
    session.add(seed)
    await session.commit()
    return {"id": seed.id}


@router.put("/{seed_id}")
async def update_seed(
    seed_id: int,
    body: SeedUpdate,
    session: AsyncSession = Depends(get_db_session),
    _=Depends(require_auth),
):
    seed = (await session.execute(
        select(CaptionSeed).where(CaptionSeed.id == seed_id)
    )).scalar_one_or_none()
    if not seed:
        raise HTTPException(404)
    if body.category is not None:
        seed.category = body.category
    if body.seed_prompt is not None:
        seed.seed_prompt = body.seed_prompt
    if body.language is not None:
        seed.language = body.language
    if body.is_active is not None:
        seed.is_active = body.is_active
    if body.model is not None:
        seed.model = body.model or None
    await session.commit()
    return {"ok": True}


@router.delete("/{seed_id}")
async def delete_seed(
    seed_id: int,
    session: AsyncSession = Depends(get_db_session),
    _=Depends(require_auth),
):
    seed = (await session.execute(
        select(CaptionSeed).where(CaptionSeed.id == seed_id)
    )).scalar_one_or_none()
    if not seed:
        raise HTTPException(404)
    seed.is_active = False
    await session.commit()
    return {"ok": True}


@router.post("/test")
async def test_generate(
    body: SeedCreate,
    config: VPSConfig = Depends(get_config),
    _=Depends(require_auth),
):
    """Test caption generation: sends seed to Grok and returns preview."""
    from server.llm_client import _call_openai_compat, _call_anthropic, PROVIDER_DEFAULTS

    system_prompt = (
        "You are a social media manager for an AI OFM model. "
        "Reply with ONLY the final caption (1-3 sentences + emojis). No hashtags."
    )
    user_prompt = (
        f"Write a carousel caption for 4 photos. "
        f"Direction: {body.seed_prompt}"
    )
    try:
        provider = config.llm_provider or "grok"
        base_url = config.llm_base_url or PROVIDER_DEFAULTS.get(provider, {}).get("base_url", "")
        if provider == "anthropic":
            result = await _call_anthropic(
                base_url, config.llm_api_key, config.llm_model,
                system_prompt, user_prompt, 30.0,
            )
        else:
            result = await _call_openai_compat(
                base_url, config.llm_api_key, config.llm_model,
                system_prompt, user_prompt, 30.0,
            )
        return {"caption": result.strip() if result else config.carousel_caption_fallback}
    except Exception as e:
        # Successful fallback path → WARNING, not ERROR.
        logger.warning(
            "Test caption generation failed (%s: %s); using fallback",
            type(e).__name__, e,
        )
        return {"caption": config.carousel_caption_fallback, "error": str(e)}
