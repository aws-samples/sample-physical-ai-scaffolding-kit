"""Tests for physai.schema validation."""

import pytest

from physai.schema import validate

# ── Valid configs pass ──


def test_valid_cli_config():
    validate({"host": "physai-login", "model_config_roots": ["/tmp"]}, "cli-config")


def test_valid_project():
    validate({"base_image": "nvcr.io/foo"}, "project")


def test_valid_container():
    validate({"name": "my-container", "base_image": "nvcr.io/foo"}, "container")


def test_valid_run_config():
    validate(
        {
            "pipeline": {"stages": ["train"]},
            "model": {"name": "gr00t", "config_dir": "gr00t/so101"},
            "stages": {"train": {"container": "tr"}},
        },
        "run-config",
    )


# ── Unknown keys rejected (additionalProperties: false) ──


def test_cli_config_unknown_key():
    with pytest.raises(SystemExit, match="cli-config"):
        validate({"host": "x", "typo_key": "y"}, "cli-config")


# ── Wrong types rejected ──


def test_container_name_wrong_type():
    with pytest.raises(SystemExit, match="container"):
        validate({"name": 123}, "container")


# ── Missing required fields rejected ──


def test_container_missing_name():
    with pytest.raises(SystemExit, match="container"):
        validate({}, "container")


def test_run_config_missing_model():
    with pytest.raises(SystemExit, match="run-config"):
        validate({"stages": {"train": {"container": "tr"}}}, "run-config")
