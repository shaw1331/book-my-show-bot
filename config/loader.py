from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from config.schema import AppConfig

logger = logging.getLogger(__name__)


def _resolve_env_vars(data: dict) -> dict:
    """Recursively resolve keys ending with '_env' to their env var values."""
    resolved = {}
    for key, value in data.items():
        if isinstance(value, dict):
            resolved[key] = _resolve_env_vars(value)
        elif isinstance(value, list):
            resolved[key] = [
                _resolve_env_vars(item) if isinstance(item, dict) else item
                for item in value
            ]
        elif isinstance(value, str) and key.endswith("_env") and value:
            # Try as env var name first; if not set, use the raw value
            # (allows pasting tokens directly in config)
            env_value = os.environ.get(value)
            if env_value:
                resolved[key] = env_value
            else:
                resolved[key] = value
        else:
            resolved[key] = value
    return resolved


def load_config(path: str | Path) -> AppConfig:
    """Load and validate configuration from a YAML file."""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    # Load .env from the same directory as the config file
    env_path = config_path.parent / ".env"
    load_dotenv(env_path)

    with open(config_path) as f:
        raw = yaml.safe_load(f)

    if not raw or not isinstance(raw, dict):
        raise ValueError(f"Config file is empty or invalid: {config_path}")

    resolved = _resolve_env_vars(raw)
    return AppConfig(**resolved)
