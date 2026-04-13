"""Load ~/.physai/config.yaml and merge with CLI flags."""

from pathlib import Path

import yaml

CONFIG_PATH = Path.home() / ".physai" / "config.yaml"

DEFAULTS = {
    "host": None,
    "s3_bucket": None,
    "model_config_roots": [],
}


def load(host_override: str | None = None) -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            file_cfg = yaml.safe_load(f) or {}
        cfg.update(file_cfg)
    if host_override:
        cfg["host"] = host_override
    if not cfg["host"]:
        raise SystemExit(
            "No host configured. Set 'host' in ~/.physai/config.yaml or pass --host."
        )
    return cfg
