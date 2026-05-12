from __future__ import annotations

import os
from pathlib import Path

import yaml


CONFIG_DIR = Path.home() / ".deployforge"
CONFIG_FILE = CONFIG_DIR / "config.yml"

DEFAULT_CONFIG: dict[str, str] = {
    "api_endpoint": "https://api.deployforge.dev",
    "default_output": "./Dockerfile",
    "format": "human",
}


def load_config() -> dict[str, str]:
    if not CONFIG_FILE.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)

    with open(CONFIG_FILE) as f:
        data = yaml.safe_load(f) or {}

    merged = {**DEFAULT_CONFIG, **data}
    return merged


def save_config(config: dict[str, str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)


def get_api_key() -> str:
    key = os.environ.get("DEPLOYFORGE_API_KEY") or load_config().get("api_key")
    if not key:
        raise SystemExit(
            "API anahtarı bulunamadı. Lütfen önce giriş yapın:\n"
            "  deployforge auth login"
        )
    return key


def get_api_endpoint() -> str:
    return load_config().get("api_endpoint", DEFAULT_CONFIG["api_endpoint"])
