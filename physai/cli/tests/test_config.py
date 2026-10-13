"""Tests for physai config loading."""

import pytest

from physai.config import load


def test_load_with_host_override(tmp_path, monkeypatch):
    monkeypatch.setattr("physai.config.CONFIG_PATH", tmp_path / "nonexistent.yaml")
    cfg = load(host_override="myhost")
    assert cfg["host"] == "myhost"


def test_load_from_file(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: filehost\n")
    monkeypatch.setattr("physai.config.CONFIG_PATH", config_file)
    cfg = load()
    assert cfg["host"] == "filehost"


def test_load_override_beats_file(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: filehost\n")
    monkeypatch.setattr("physai.config.CONFIG_PATH", config_file)
    cfg = load(host_override="clihost")
    assert cfg["host"] == "clihost"


def test_load_no_host_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("physai.config.CONFIG_PATH", tmp_path / "nonexistent.yaml")
    with pytest.raises(SystemExit):
        load()


def test_load_data_bucket(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: filehost\ndata_bucket: my-bucket\n")
    monkeypatch.setattr("physai.config.CONFIG_PATH", config_file)
    cfg = load()
    assert cfg["data_bucket"] == "my-bucket"


def test_load_data_bucket_defaults_to_none(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: filehost\n")
    monkeypatch.setattr("physai.config.CONFIG_PATH", config_file)
    cfg = load()
    assert cfg["data_bucket"] is None
