"""Tests for LLM fallback hardening at every call site.

The invariant we lock in here: when the LLM provider is unset (or the
provider errors out), none of the scheduler helpers / API endpoints / bots
may raise — each must degrade to a static fallback so the farm keeps
posting.

Covers the 9 call sites Codex enumerated:

1. scheduler._generate_carousel_caption — fallback when provider unset
2. scheduler._generate_carousel_caption — fallback when LLM raises
3. scheduler._generate_story_element — fallback when LLM raises
4. models_api._vision_describe — placeholder on HTTP error / provider unset
5. channel_bot.generator.generate_post — None when provider unset
6. channel_bot.generator.generate_poll — None when provider unset
7. channel_bot.generator.generate_paid_caption — fallback when provider unset
8. direct_bot._ask_llm — None when provider unset
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from server.channel_bot.config import ChannelBotConfig, ChannelConfig
from server.channel_bot.generator import (
    _DEFAULT_PAID_CAPTION_FALLBACK,
    generate_paid_caption,
    generate_poll,
    generate_post,
)
from server.config import VPSConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def llm_off_config() -> VPSConfig:
    """Farm config with no LLM provider → every helper must use its fallback."""
    return VPSConfig(
        llm_provider="",
        llm_api_key="",
        llm_base_url="",
        llm_model="",
        carousel_caption_fallback="PICK YOUR FAVE",
        story_poll_fallback="Hot or not? | Hot | Not",
        story_question_fallback="Ask me anything darling",
        channel_bot_paid_caption_fallback="PAID FALLBACK",
        farm_llm_required=False,
    )


@pytest.fixture
def llm_on_config() -> VPSConfig:
    """Farm config with an LLM provider set so we can exercise the
    try/except fallback path (mock the call to raise)."""
    return VPSConfig(
        llm_provider="grok",
        llm_api_key="fake-key",
        llm_base_url="http://localhost:0",
        llm_model="fake-model",
        carousel_caption_fallback="PICK YOUR FAVE",
        story_poll_fallback="Hot or not? | Hot | Not",
        story_question_fallback="Ask me anything darling",
    )


def _fake_scheduler(config: VPSConfig):
    """Build a bare-minimum object that looks like FarmScheduler for
    _generate_carousel_caption / _generate_story_element.

    Both helpers call ``self.session_factory()`` inside a try/except
    that swallows any exception (for seed_text lookup), so a
    session_factory that raises on call is tolerated. The helpers
    only really need ``self.config`` wired up.
    """
    from server.scheduler import FarmScheduler

    fake = FarmScheduler.__new__(FarmScheduler)
    fake.config = config

    def _broken_session_factory():  # pragma: no cover - never reached in happy path
        raise RuntimeError("no db in this test")

    fake.session_factory = _broken_session_factory
    return fake


# ---------------------------------------------------------------------------
# 1-3. scheduler._generate_carousel_caption + _generate_story_element
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_carousel_caption_returns_fallback_when_provider_unset(
    llm_off_config: VPSConfig,
) -> None:
    """No llm_provider → return config.carousel_caption_fallback without raising."""
    scheduler = _fake_scheduler(llm_off_config)

    caption = await scheduler._generate_carousel_caption(
        username="test_user",
        photo_count=4,
        tags=["indoor", "elegant"],
    )

    assert caption == "PICK YOUR FAVE"


@pytest.mark.asyncio
async def test_carousel_caption_returns_fallback_when_llm_raises(
    llm_on_config: VPSConfig,
) -> None:
    """Provider set but LLM raises → catch and return the static fallback."""
    scheduler = _fake_scheduler(llm_on_config)

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated LLM outage")

    # Patch the helpers inside the scheduler module where they're actually
    # re-imported. _generate_carousel_caption does `from server.llm_client
    # import _call_openai_compat, _call_anthropic` inside the function, so
    # patching the source module is sufficient.
    with patch("server.llm_client._call_openai_compat", new=_boom), \
         patch("server.llm_client._call_anthropic", new=_boom):
        caption = await scheduler._generate_carousel_caption(
            username="test_user",
            photo_count=4,
            tags=["indoor"],
        )

    assert caption == "PICK YOUR FAVE"


@pytest.mark.asyncio
async def test_story_element_returns_fallback_when_llm_raises(
    llm_on_config: VPSConfig,
) -> None:
    """Story element helper catches LLM failure and falls back per type."""
    scheduler = _fake_scheduler(llm_on_config)
    # Force element probability to 1.0 so the helper always picks a sticker.
    scheduler.config.story_element_probability = 1.0

    async def _boom(*args, **kwargs):
        raise httpx.ConnectError("cannot connect")

    with patch("server.llm_client._call_openai_compat", new=_boom), \
         patch("server.llm_client._call_anthropic", new=_boom):
        element = await scheduler._generate_story_element(username="test_user")

    assert element is not None
    assert element["type"] in ("poll", "question")
    if element["type"] == "poll":
        # story_poll_fallback = "Hot or not? | Hot | Not" → question "Hot or not?"
        assert element["text"] == "Hot or not?"
        assert element["options"] == ["Hot", "Not"]
    else:
        assert element["text"] == "Ask me anything darling"


@pytest.mark.asyncio
async def test_story_element_returns_fallback_when_provider_unset(
    llm_off_config: VPSConfig,
) -> None:
    """No LLM provider → still produces a sticker from the static fallback."""
    scheduler = _fake_scheduler(llm_off_config)
    scheduler.config.story_element_probability = 1.0

    element = await scheduler._generate_story_element(username="test_user")

    assert element is not None
    assert element["type"] in ("poll", "question")


# ---------------------------------------------------------------------------
# 4. models_api._vision_describe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vision_describe_returns_placeholder_on_http_error(
    llm_on_config: VPSConfig,
) -> None:
    """httpx raising a connect error → return placeholder, no raise."""
    from server.api.models_api import _vision_describe

    class _BrokenClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.ConnectError("cannot connect")

    with patch("httpx.AsyncClient", new=lambda *a, **kw: _BrokenClient()):
        result = await _vision_describe(llm_on_config, image_b64="fake")

    assert result == {"description": "(LLM unavailable)", "tags": []}


@pytest.mark.asyncio
async def test_vision_describe_returns_placeholder_when_provider_unset(
    llm_off_config: VPSConfig,
) -> None:
    """No provider → fast-path placeholder without touching httpx at all."""
    from server.api.models_api import _vision_describe

    async def _should_not_be_called(*args, **kwargs):  # pragma: no cover
        raise AssertionError("httpx must not be called when LLM unset")

    with patch("httpx.AsyncClient", new=_should_not_be_called):
        result = await _vision_describe(llm_off_config, image_b64="fake")

    assert result == {"description": "(LLM unavailable)", "tags": []}


@pytest.mark.asyncio
async def test_vision_describe_returns_placeholder_on_timeout(
    llm_on_config: VPSConfig,
) -> None:
    """Timeout / 500 responses → placeholder, no 502."""
    from server.api.models_api import _vision_describe

    class _TimeoutClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.ReadTimeout("upstream timeout")

    with patch("httpx.AsyncClient", new=lambda *a, **kw: _TimeoutClient()):
        result = await _vision_describe(llm_on_config, image_b64="fake")

    assert result["description"] == "(LLM unavailable)"
    assert result["tags"] == []


# ---------------------------------------------------------------------------
# 5-7. channel_bot.generator.generate_post / generate_poll / generate_paid_caption
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_post_returns_none_when_provider_unset() -> None:
    """LLM unset → generate_post returns None, does NOT raise."""
    cfg = ChannelBotConfig(llm_provider="")
    ch = ChannelConfig(channel_id="test", legend="Test legend")

    result = await generate_post(cfg, ch, db_path="")

    assert result is None


@pytest.mark.asyncio
async def test_generate_poll_returns_none_when_provider_unset() -> None:
    """LLM unset → generate_poll returns None, does NOT raise."""
    cfg = ChannelBotConfig(llm_provider="")
    ch = ChannelConfig(channel_id="test", legend="Test legend")

    result = await generate_poll(cfg, ch, db_path="")

    assert result is None


@pytest.mark.asyncio
async def test_generate_paid_caption_returns_fallback_when_provider_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM unset → generate_paid_caption returns the configured fallback."""
    cfg = ChannelBotConfig(llm_provider="")
    ch = ChannelConfig(channel_id="test", legend="Test legend")

    # Stub out load_config so the test is hermetic (no real config.yaml).
    from server import config as vps_config_module

    def _fake_vps_config() -> VPSConfig:
        return VPSConfig(channel_bot_paid_caption_fallback="CUSTOM PAID TEASER")

    monkeypatch.setattr(vps_config_module, "load_config", _fake_vps_config)

    result = await generate_paid_caption(cfg, ch, db_path=None)

    assert result == "CUSTOM PAID TEASER"


@pytest.mark.asyncio
async def test_generate_paid_caption_uses_default_when_vps_config_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VPS config.yaml missing → fall back to the built-in constant."""
    cfg = ChannelBotConfig(llm_provider="")
    ch = ChannelConfig(channel_id="test", legend="Test legend")

    def _boom() -> VPSConfig:
        raise FileNotFoundError("no config.yaml here")

    from server import config as vps_config_module
    monkeypatch.setattr(vps_config_module, "load_config", _boom)

    result = await generate_paid_caption(cfg, ch, db_path=None)

    assert result == _DEFAULT_PAID_CAPTION_FALLBACK


# ---------------------------------------------------------------------------
# 8. direct_bot._ask_llm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_direct_bot_ask_llm_returns_none_when_provider_unset() -> None:
    """DirectBot with no llm_provider → _ask_llm returns None (callers check)."""
    from server.direct_bot import DirectBot

    bot = DirectBot.__new__(DirectBot)
    bot.llm_provider = ""
    bot.llm_base_url = ""
    bot.llm_api_key = ""
    bot.llm_model = ""
    bot.llm_timeout = 30.0

    result = await bot._ask_llm(prompt="hello")

    assert result is None


# ---------------------------------------------------------------------------
# Retry wrapper unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_call_with_retry_retries_transient_status() -> None:
    """_llm_call_with_retry retries once on 503 and returns the second result."""
    from server.scheduler import _llm_call_with_retry

    calls: list[int] = []

    class _FakeResp:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code

    async def _flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.HTTPStatusError(
                "503",
                request=MagicMock(),
                response=_FakeResp(503),  # type: ignore[arg-type]
            )
        return "second-try-ok"

    result = await _llm_call_with_retry(_flaky)
    assert result == "second-try-ok"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_llm_call_with_retry_does_not_retry_auth_error() -> None:
    """401 Unauthorized is non-transient → raised immediately, no retry."""
    from server.scheduler import _llm_call_with_retry

    calls: list[int] = []

    class _FakeResp:
        status_code = 401

    async def _auth_failure(*args, **kwargs):
        calls.append(1)
        raise httpx.HTTPStatusError(
            "401",
            request=MagicMock(),
            response=_FakeResp(),  # type: ignore[arg-type]
        )

    with pytest.raises(httpx.HTTPStatusError):
        await _llm_call_with_retry(_auth_failure)
    assert len(calls) == 1  # no retry


@pytest.mark.asyncio
async def test_llm_call_with_retry_retries_timeout() -> None:
    """TimeoutException retried once; second success returns."""
    from server.scheduler import _llm_call_with_retry

    calls: list[int] = []

    async def _slow_then_fast(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow")
        return "ok"

    result = await _llm_call_with_retry(_slow_then_fast)
    assert result == "ok"
    assert len(calls) == 2
