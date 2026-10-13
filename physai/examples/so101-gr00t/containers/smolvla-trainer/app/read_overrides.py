"""Flatten a YAML mapping into `--key=value` strings for lerobot-train.

Reads `<model_config_dir>/train_overrides.yaml` (optional). Nested mappings
are joined with `.` so a file like

    policy:
      freeze_vision_encoder: true

becomes `--policy.freeze_vision_encoder=true`. Booleans go lowercase; lists
and dicts are emitted as JSON (matches how lerobot-train parses them).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml


def _flatten(prefix: str, value: Any, out: list[str]) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            sub = f"{prefix}.{k}" if prefix else str(k)
            _flatten(sub, v, out)
        return
    if isinstance(value, bool):
        out.append(f"--{prefix}={'true' if value else 'false'}")
        return
    if isinstance(value, (list, tuple)):
        out.append(f"--{prefix}={json.dumps(list(value))}")
        return
    out.append(f"--{prefix}={value}")


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: read_overrides.py <overrides.yaml>", file=sys.stderr)
        return 2

    path = Path(sys.argv[1])
    if not path.exists():
        return 0

    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        print(f"ERROR: {path} must be a mapping", file=sys.stderr)
        return 1

    args: list[str] = []
    for k, v in data.items():
        _flatten(str(k), v, args)
    for a in args:
        print(a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
