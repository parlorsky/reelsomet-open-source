"""Checks for the public distribution's first-run and static-serving contract."""
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server.app import create_app
from server.config import load_config, save_config
from server.init_config import initialize


def test_initialize_is_private_and_preserves_existing_settings(tmp_path: Path):
    path = tmp_path / 'var' / 'config.yaml'
    config = initialize(path)
    assert config.setup_token
    assert not config.admin_password_hash
    assert not config.ghost_enabled
    assert not config.farm_use_scenarios_default
    assert path.stat().st_mode & 0o077 == 0
    assert (Path(config.data_dir) / 'fsm' / 'posting.json').is_file()
    config.port = 9123
    token = config.setup_token
    save_config(config)
    restored = initialize(path)
    assert restored.port == 9123
    assert restored.setup_token == token
    assert load_config(path).database_path == config.database_path


@pytest.mark.asyncio
async def test_spa_routes_do_not_mask_api_errors(tmp_path: Path):
    config = initialize(tmp_path / 'config.yaml')
    static = tmp_path / 'web'
    static.mkdir()
    (static / 'index.html').write_text('<html>distribution smoke</html>')
    config.static_dir = str(static)
    app = create_app(config)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        assert (await client.get('/setup')).status_code == 200
        assert 'distribution smoke' in (await client.get('/accounts')).text
        response = await client.get('/api/does-not-exist')
        assert response.status_code == 404
        assert response.headers['content-type'].startswith('application/json')
