"""Entry point: parse CLI args, load config, create app, run uvicorn."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import uvicorn

from server.app import create_app
from server.config import VPSConfig, _resolve_config_path
from server.bootstrap_auth import ensure_setup_token
from server.init_config import initialize

logger = logging.getLogger("server")


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reelsomet VPS Server")
    parser.add_argument("--host", type=str, default=None, help="Bind host (overrides config)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (overrides config)")
    parser.add_argument("--config", type=str, default=None, help="Path to config.yaml")
    parser.add_argument("--log-level", type=str, default="INFO", help="Log level (DEBUG, INFO, WARNING, ERROR)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)
    _setup_logging(args.log_level)

    config_path = Path(args.config) if args.config else _resolve_config_path()
    config: VPSConfig = initialize(config_path)
    setup_token = ensure_setup_token(config)
    if setup_token:
        print(f"First-run setup token: {setup_token}", flush=True)

    # CLI overrides
    host = args.host or config.host
    port = args.port or config.port

    logger.info("Starting Reelsomet VPS on %s:%d", host, port)

    app = create_app(config)
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=args.log_level.lower(),
        ws_ping_interval=config.ws_ping_interval,
        ws_ping_timeout=config.ws_ping_timeout,
    )


if __name__ == "__main__":
    main()
