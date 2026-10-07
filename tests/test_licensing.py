"""The open-source edition must work without a hardware-bound activation."""
from server.config import VPSConfig
from server.license_gate import get_license_info, is_licensed


def test_empty_config_is_available_without_activation():
    config = VPSConfig(license_key="")
    assert is_licensed(config)
    info = get_license_info(config)
    assert info.max_devices == 0
    assert not info.is_expired
    assert info.hwid == ""


def test_legacy_key_does_not_change_open_source_entitlements():
    config = VPSConfig(license_key="invalid-legacy-value")
    assert get_license_info(config) == get_license_info(VPSConfig())
