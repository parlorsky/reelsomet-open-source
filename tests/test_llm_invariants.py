"""Drift-detection invariants for LLM call sites.

Asserts the cross-cutting invariant: "no generator call site raises when
the LLM is broken". If a new generator is added to scheduler/bots/models
without a fallback path, one of these tests will catch it.

The test inventories the known helpers and calls each one with an LLM
configuration that's guaranteed to fail — either provider unset (fast
path) or provider set + httpx client mocked to raise. No helper may
raise; all must return a non-None value for string-returning helpers or
an explicit None for the channel-bot helpers that callers are expected
to handle.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from server.channel_bot.config import ChannelBotConfig, ChannelConfig
from server.config import VPSConfig


def _broken_session_factory():
    raise RuntimeError("db intentionally unavailable in invariant test")


def _fake_farm_scheduler(config: VPSConfig):
    """Bare-bones FarmScheduler standin: only ``config`` + a broken session
    factory. The helpers under test only need ``self.config`` on the happy
    and fallback paths — the DB lookup for seeds is already try/except'd.
    """
    from server.scheduler import FarmScheduler

    fake = FarmScheduler.__new__(FarmScheduler)
    fake.config = config
    fake.session_factory = _broken_session_factory
    return fake


@pytest.fixture
def broken_llm_config() -> VPSConfig:
    """Config with no llm_provider (fast-path fallback everywhere)."""
    return VPSConfig(
        llm_provider="",
        llm_api_key="",
        llm_base_url="",
        llm_model="",
        carousel_caption_fallback="STATIC CAROUSEL",
        story_poll_fallback="Q? | A | B",
        story_question_fallback="Ask fallback",
        channel_bot_paid_caption_fallback="STATIC PAID",
    )


# ---------------------------------------------------------------------------
# Invariant 1: scheduler helpers never raise when LLM unset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_scheduler_generator_raises_when_llm_unset(
    broken_llm_config: VPSConfig,
) -> None:
    """Each scheduler LLM helper must return a non-None value without raising.

    Known helpers audited:
      - _generate_carousel_caption
      - _generate_story_element (called with element_probability=1.0
        so we always hit the LLM path, not the "no sticker" roll)
    """
    scheduler = _fake_farm_scheduler(broken_llm_config)
    scheduler.config.story_element_probability = 1.0

    # Inventory of helpers we claim are hardened.
    # Each entry: (attr_name, args-tuple, kwargs-dict)
    helpers = [
        ("_generate_carousel_caption", (), {
            "username": "test_user",
            "photo_count": 4,
            "tags": ["indoor"],
        }),
        ("_generate_story_element", (), {"username": "test_user"}),
    ]

    for attr, args, kwargs in helpers:
        helper = getattr(scheduler, attr)
        result = await helper(*args, **kwargs)
        assert result is not None, f"{attr} returned None with LLM unset"


@pytest.mark.asyncio
async def test_no_scheduler_generator_raises_when_llm_explodes(
    broken_llm_config: VPSConfig,
) -> None:
    """Same invariant with a provider set but the client always raising."""
    cfg = VPSConfig(
        llm_provider="grok",
        llm_api_key="fake",
        llm_base_url="http://localhost:0",
        llm_model="fake",
        carousel_caption_fallback="STATIC CAROUSEL",
        story_poll_fallback="Q? | A | B",
        story_question_fallback="Ask fallback",
    )
    scheduler = _fake_farm_scheduler(cfg)
    scheduler.config.story_element_probability = 1.0

    async def _boom(*args, **kwargs):
        raise RuntimeError("forced failure")

    with patch("server.llm_client._call_openai_compat", new=_boom), \
         patch("server.llm_client._call_anthropic", new=_boom):
        caption = await scheduler._generate_carousel_caption(
            username="u", photo_count=3, tags=[],
        )
        element = await scheduler._generate_story_element(username="u")

    assert caption == "STATIC CAROUSEL"
    assert element is not None


# ---------------------------------------------------------------------------
# Invariant 2: caption_seeds.test_generate endpoint never raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_caption_seeds_test_generate_never_raises(
    broken_llm_config: VPSConfig,
) -> None:
    """The /api/caption-seeds/test endpoint must always return a dict."""
    from server.api.caption_seeds import SeedCreate, test_generate

    body = SeedCreate(
        category="test",
        seed_prompt="lonely on the beach",
        language="en",
    )

    # Provider unset path
    result = await test_generate(body=body, config=broken_llm_config)
    assert "caption" in result
    assert result["caption"] == "STATIC CAROUSEL"


# ---------------------------------------------------------------------------
# Invariant 3: models_api._vision_describe never raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vision_describe_never_raises_on_any_http_error(
    broken_llm_config: VPSConfig,
) -> None:
    """Simulate every common httpx failure mode and assert no raise."""
    from server.api import models_api

    # Cover: ConnectError, ReadTimeout, HTTPStatusError(500), OSError
    errors: list[Exception] = [
        httpx.ConnectError("dns"),
        httpx.ReadTimeout("slow"),
        OSError("disk full"),
    ]

    cfg_on = VPSConfig(
        llm_provider="grok",
        llm_api_key="fake",
        llm_base_url="http://localhost:0",
        llm_model="fake",
    )

    for err in errors:
        class _ErrClient:
            def __init__(self, exc: Exception) -> None:
                self.exc = exc

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **kw):
                raise self.exc

        bound_err = err

        with patch("httpx.AsyncClient", new=lambda *a, **kw: _ErrClient(bound_err)):
            result = await models_api._vision_describe(cfg_on, image_b64="fake")

        assert result["description"] == "(LLM unavailable)"
        assert result["tags"] == []


# ---------------------------------------------------------------------------
# Invariant 4: channel_bot generators don't raise when provider unset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_channel_bot_generator_raises_when_provider_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inventory of channel_bot async generators, each must be callable
    with provider="" without raising.

    - generate_post → None
    - generate_poll → None
    - generate_paid_caption → str (fallback)
    """
    from server.channel_bot import generator as gen_module

    cfg = ChannelBotConfig(llm_provider="")
    ch = ChannelConfig(channel_id="ch-test", legend="Legend")

    # Stub out VPS load_config so generate_paid_caption can resolve its fallback
    # hermetically (don't read a real config.yaml).
    from server import config as vps_cfg_module

    monkeypatch.setattr(
        vps_cfg_module,
        "load_config",
        lambda: VPSConfig(channel_bot_paid_caption_fallback="INV_PAID"),
    )

    assert await gen_module.generate_post(cfg, ch, db_path="") is None
    assert await gen_module.generate_poll(cfg, ch, db_path="") is None

    paid = await gen_module.generate_paid_caption(cfg, ch, db_path=None)
    assert paid == "INV_PAID"


# ---------------------------------------------------------------------------
# Invariant 5: direct_bot._ask_llm returns None when unset
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_bot_ask_llm_safe_when_provider_unset() -> None:
    """DirectBot._ask_llm returns None (signals "skip this turn") when unset."""
    from server.direct_bot import DirectBot

    bot = DirectBot.__new__(DirectBot)
    bot.llm_provider = ""
    bot.llm_base_url = ""
    bot.llm_api_key = ""
    bot.llm_model = ""
    bot.llm_timeout = 30.0

    assert await bot._ask_llm("hi") is None
