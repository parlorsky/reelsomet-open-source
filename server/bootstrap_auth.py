"""One-use bootstrap credentials for the first administrator."""
import secrets
from server.config import VPSConfig, save_config


def ensure_setup_token(config: VPSConfig) -> str:
    """Persist a token once; never re-enable setup on a configured server."""
    if config.admin_password_hash:
        return ""
    if not config.setup_token:
        config.setup_token = secrets.token_urlsafe(32)
        save_config(config)
    return config.setup_token
