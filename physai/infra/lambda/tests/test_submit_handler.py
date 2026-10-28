"""Tests for the submit Lambda's entry point. Every AWS and SSH call is mocked."""

from unittest.mock import MagicMock, patch

import pytest


def test_an_unprovisioned_key_is_reported_not_raised(submit):
    """Expected until authorized_keys is set up, and it proves the network path."""
    with patch.object(
        submit, "_on_cluster", side_effect=submit.paramiko.AuthenticationException
    ):
        response = submit._run_probe({})
    assert response["ok"] is False
    assert response["reason"] == "authenticationFailed"


def test_missing_environment_is_named(submit, monkeypatch):
    for key in submit.CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="CLUSTER_NAME"):
        submit._load_config()


def test_an_unknown_payload_is_refused(submit, monkeypatch):
    for key in submit.CONFIG_KEYS:
        monkeypatch.setenv(key, "x")
    response = submit.lambda_handler({}, MagicMock())
    assert response["ok"] is False
    assert "probeOnly" in response["message"]
