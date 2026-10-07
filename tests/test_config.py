"""Tests for server.config: VPSConfig, load_config, save_config."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from server.bootstrap_auth import ensure_setup_token
from server.config import VPSConfig, _get_nested, _set_nested, load_config, save_config


# ---------------------------------------------------------------------------
# VPSConfig defaults
# ---------------------------------------------------------------------------

class TestVPSConfigDefaults:

    def test_default_config_values(self) -> None:
        """VPSConfig() has sensible defaults for all fields."""
        cfg = VPSConfig()
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 8000
        assert cfg.api_key == ""
        assert cfg.ws_ping_interval == 30.0
        assert cfg.ws_ping_timeout == 10.0
        assert cfg.database_path == "var/data/db/farm.db"
        assert cfg.data_dir == "var/data"
        assert cfg.static_dir == "web/dist"
        assert cfg.domain == ""
        assert cfg.admin_password_hash == ""
        # jwt_secret is auto-generated, just check it's non-empty
        assert len(cfg.jwt_secret) > 0
        assert cfg.jwt_expire_hours == 24
        assert cfg.setup_token == ""
        assert cfg.telegram_bot_token == ""
        assert cfg.telegram_admin_chat_ids == []
        assert cfg.llm_base_url == ""
        assert cfg.llm_api_key == ""
        assert cfg.llm_model == ""
        assert cfg.engagement_llm_timeout == 30.0
        assert cfg.farm_timezone == "Europe/Moscow"

    def test_jwt_secret_unique_per_instance(self) -> None:
        """Each new VPSConfig gets a unique jwt_secret."""
        c1 = VPSConfig()
        c2 = VPSConfig()
        assert c1.jwt_secret != c2.jwt_secret

    def test_telegram_admin_chat_ids_not_shared(self) -> None:
        """Mutable default (list) is not shared between instances."""
        c1 = VPSConfig()
        c2 = VPSConfig()
        c1.telegram_admin_chat_ids.append(999)
        assert 999 not in c2.telegram_admin_chat_ids


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:

    def test_load_config_from_yaml(self, tmp_config_path: Path) -> None:
        """Load a full YAML config and verify all fields map correctly."""
        cfg = load_config(tmp_config_path)
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 9999
        assert cfg.api_key == "test-api-key"
        assert cfg.ws_ping_interval == 15.0
        assert cfg.ws_ping_timeout == 5.0
        assert cfg.database_path == "/tmp/test-farm.db"
        assert cfg.data_dir == "/tmp/test-data"
        assert cfg.static_dir == "/tmp/test-static"
        assert cfg.domain == "test.example.com"
        assert cfg.jwt_secret == "test-jwt-secret-value-for-tests"
        assert cfg.jwt_expire_hours == 2
        assert cfg.setup_token == "sample-setup-token"
        assert cfg.telegram_bot_token == "123456:ABC-DEF"
        assert cfg.telegram_admin_chat_ids == [111, 222]
        assert cfg.llm_base_url == "http://localhost:11434"
        assert cfg.llm_api_key == "test-llm-key"
        assert cfg.llm_model == "test-model"
        assert cfg.engagement_llm_timeout == 15.0
        assert cfg.farm_timezone == "Europe/Moscow"

    def test_load_config_stores_path(self, tmp_config_path: Path) -> None:
        """_config_path records which file was loaded."""
        cfg = load_config(tmp_config_path)
        assert cfg._config_path == str(tmp_config_path)

    def test_load_config_missing_file(self, tmp_path: Path) -> None:
        """When the config file doesn't exist, return defaults."""
        missing = tmp_path / "nonexistent.yaml"
        cfg = load_config(missing)
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 8000
        assert cfg.database_path == "var/data/db/farm.db"
        assert cfg._config_path == str(missing)

    def test_load_config_partial_yaml(self, tmp_path: Path) -> None:
        """Missing YAML keys use defaults; present keys are loaded."""
        partial = tmp_path / "partial.yaml"
        with open(partial, "w") as f:
            yaml.dump({"server": {"host": "10.0.0.1", "port": 7777}}, f)
        cfg = load_config(partial)
        assert cfg.host == "10.0.0.1"
        assert cfg.port == 7777
        # Other fields remain default
        assert cfg.database_path == "var/data/db/farm.db"
        assert cfg.jwt_expire_hours == 24
        assert cfg.telegram_bot_token == ""

    def test_load_config_empty_yaml(self, tmp_path: Path) -> None:
        """An empty YAML file should produce all defaults."""
        empty = tmp_path / "empty.yaml"
        empty.write_text("")
        cfg = load_config(empty)
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 8000

    def test_load_config_int_from_float(self, tmp_path: Path) -> None:
        """YAML may produce a float for integers (e.g. 8001.0). Config coerces to int."""
        p = tmp_path / "float_port.yaml"
        with open(p, "w") as f:
            yaml.dump({"server": {"port": 8001.0}}, f)
        cfg = load_config(p)
        assert cfg.port == 8001
        assert isinstance(cfg.port, int)

    def test_config_env_var_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """REELSOMET_DATA_DIR changes the resolved config path."""
        from server.config import _resolve_config_path

        monkeypatch.setenv("REELSOMET_DATA_DIR", str(tmp_path))
        resolved = _resolve_config_path()
        assert resolved == tmp_path / "config.yaml"

    def test_config_env_var_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without REELSOMET_DATA_DIR, default path is used."""
        from server.config import _DEFAULT_CONFIG_PATH, _resolve_config_path

        monkeypatch.delenv("REELSOMET_DATA_DIR", raising=False)
        resolved = _resolve_config_path()
        assert resolved == _DEFAULT_CONFIG_PATH


# ---------------------------------------------------------------------------
# save_config
# ---------------------------------------------------------------------------

class TestSaveConfig:

    def test_save_config_roundtrip(self, tmp_path: Path) -> None:
        """Save then reload a config — all fields preserved."""
        out = tmp_path / "saved.yaml"
        original = VPSConfig(
            host="1.2.3.4",
            port=5555,
            api_key="my-key",
            database_path="/db/path.db",
            data_dir="/my/data",
            static_dir="/my/static",
            domain="my.domain.com",
            jwt_secret="fixed-secret",
            jwt_expire_hours=48,
            setup_token="bootstrap-token",
            telegram_bot_token="tok:en",
            telegram_admin_chat_ids=[10, 20, 30],
            llm_base_url="http://llm:1234",
            llm_api_key="llm-key",
            llm_model="gpt-test",
            engagement_llm_timeout=60.0,
            farm_timezone="Europe/Moscow",
        )
        original._config_path = str(out)
        save_config(original, out)

        assert out.exists()
        reloaded = load_config(out)
        assert reloaded.host == "1.2.3.4"
        assert reloaded.port == 5555
        assert reloaded.api_key == "my-key"
        assert reloaded.database_path == "/db/path.db"
        assert reloaded.domain == "my.domain.com"
        assert reloaded.jwt_secret == "fixed-secret"
        assert reloaded.jwt_expire_hours == 48
        assert reloaded.setup_token == "bootstrap-token"
        assert reloaded.telegram_bot_token == "tok:en"
        assert reloaded.telegram_admin_chat_ids == [10, 20, 30]
        assert reloaded.llm_base_url == "http://llm:1234"
        assert reloaded.llm_model == "gpt-test"
        assert reloaded.engagement_llm_timeout == 60.0
        assert reloaded.farm_timezone == "Europe/Moscow"

    def test_save_config_creates_parent_dirs(self, tmp_path: Path) -> None:
        """save_config creates intermediate directories."""
        deep = tmp_path / "a" / "b" / "c" / "config.yaml"
        cfg = VPSConfig(host="deep-host")
        save_config(cfg, deep)
        assert deep.exists()
        reloaded = load_config(deep)
        assert reloaded.host == "deep-host"

    def test_save_config_preserves_extra_keys(self, tmp_path: Path) -> None:
        """Existing YAML keys outside our schema are preserved after save."""
        p = tmp_path / "extra.yaml"
        with open(p, "w") as f:
            yaml.dump({"custom_section": {"key": "value"}, "server": {"host": "old"}}, f)

        cfg = load_config(p)
        cfg.host = "new-host"
        save_config(cfg, p)

        with open(p) as f:
            raw = yaml.safe_load(f)
        assert raw["custom_section"]["key"] == "value"
        assert raw["server"]["host"] == "new-host"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

class TestNestedHelpers:

    def test_get_nested_basic(self) -> None:
        d = {"a": {"b": {"c": 42}}}
        assert _get_nested(d, "a.b.c") == 42

    def test_get_nested_missing_raises(self) -> None:
        d = {"a": {"b": 1}}
        with pytest.raises(KeyError):
            _get_nested(d, "a.x.y")

    def test_set_nested_creates_intermediates(self) -> None:
        d: dict = {}
        _set_nested(d, "x.y.z", 99)
        assert d == {"x": {"y": {"z": 99}}}

    def test_set_nested_overwrites(self) -> None:
        d = {"x": {"y": "old"}}
        _set_nested(d, "x.y", "new")
        assert d["x"]["y"] == "new"


class TestBootstrapAuth:

    def test_ensure_setup_token_generates_and_persists(self, tmp_path: Path) -> None:
        """Unconfigured installs receive a persisted setup token."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("", encoding="utf-8")
        cfg = VPSConfig(_config_path=str(config_path))

        token = ensure_setup_token(cfg)

        assert token
        assert cfg.setup_token == token
        reloaded = load_config(config_path)
        assert reloaded.setup_token == token
