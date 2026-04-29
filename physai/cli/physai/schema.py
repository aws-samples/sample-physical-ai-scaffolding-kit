"""JSON Schema validation for YAML config files."""

import json
from pathlib import Path

import jsonschema

_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


def validate(data: dict, schema_name: str, config_path: str = "") -> None:
    """Validate *data* against *schema_name* (e.g. 'container').

    Raises ``SystemExit`` with a human-readable message on failure.
    """
    schema_file = _SCHEMA_DIR / f"{schema_name}.schema.json"
    schema = json.loads(schema_file.read_text())
    try:
        jsonschema.validate(data, schema)
    except jsonschema.ValidationError as exc:
        label = f" ({config_path})" if config_path else ""
        raise SystemExit(f"{schema_name}{label}: {exc.message}") from None
