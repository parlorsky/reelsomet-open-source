"""Generate an isolated configuration for a new local or container install."""
import argparse
import os
import shutil
from pathlib import Path
from server.bootstrap_auth import ensure_setup_token
from server.config import VPSConfig, load_config, save_config


def initialize(path: Path) -> VPSConfig:
    path = path.expanduser().resolve()
    if path.exists():
        return load_config(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data_dir = path.parent / "data"
    config = VPSConfig(
        host=os.environ.get("REELSOMET_HOST", "127.0.0.1"),
        data_dir=str(data_dir),
        database_path=str(data_dir / "db" / "farm.db"),
        static_dir=str(Path("web/dist").resolve()),
        farm_timezone=os.environ.get("TZ", "UTC"),
        ghost_enabled=False,
        farm_use_scenarios_default=False,
        _config_path=str(path),
    )
    save_config(config)
    source = Path(__file__).resolve().parent.parent / "data" / "fsm"
    if source.is_dir():
        shutil.copytree(source, data_dir / "fsm", dirs_exist_ok=True)
    ensure_setup_token(config)
    return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("var/config.yaml"))
    args = parser.parse_args()
    config = initialize(args.config)
    token = ensure_setup_token(config)
    print(f"Configuration: {config._config_path}")
    if token:
        print(f"Setup token (paste into the first-run form): {token}")
    else:
        print("Administrator already configured; existing settings preserved.")


if __name__ == "__main__":
    main()
