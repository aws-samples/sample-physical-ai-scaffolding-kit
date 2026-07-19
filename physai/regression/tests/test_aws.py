"""Tests for the boto3 client factory in ``physai_regression.orchestration.aws``.

These pin the wiring the region-correctness fix depends on: that ``s3_client``
selects the SigV4 + virtual-addressing config only ``for_presign``, that
``region`` is threaded in as ``region_name`` (the signing region), and that
``profile`` reaches the boto3 Session. Everywhere else in the suite this
factory is mocked, so without these tests a refactor that dropped the presign
config or the region wiring would go uncaught.
"""

from unittest.mock import MagicMock, patch

from botocore.config import Config

from physai_regression.orchestration import aws


def _patched_session():
    """Patch boto3's Session so .client(...) calls are captured.

    Returns (session_ctor_patch, session_instance) — the instance's
    ``client`` is a MagicMock whose call args the tests inspect.
    """
    session = MagicMock()
    return patch(
        "physai_regression.orchestration.aws.boto3.session.Session",
        return_value=session,
    ), session


def test_s3_client_threads_profile_and_region_without_presign_config():
    ctor, session = _patched_session()
    with ctor as make_session:
        client = aws.s3_client("myprofile", "ap-northeast-1")

    make_session.assert_called_once_with(profile_name="myprofile")
    session.client.assert_called_once()
    args, kwargs = session.client.call_args
    assert args[0] == "s3"
    assert kwargs["region_name"] == "ap-northeast-1"
    # Default (non-presign) client must NOT carry the SigV4/virtual config.
    assert kwargs["config"] is None
    assert client is session.client.return_value


def test_s3_client_for_presign_uses_sigv4_virtual_config():
    ctor, session = _patched_session()
    with ctor:
        aws.s3_client("p", "us-west-2", for_presign=True)

    _, kwargs = session.client.call_args
    config = kwargs["config"]
    assert isinstance(config, Config)
    # The two properties that make a presigned URL region-correct.
    assert config.signature_version == "s3v4"
    assert config.s3["addressing_style"] == "virtual"
    assert kwargs["region_name"] == "us-west-2"


def test_s3_client_passes_none_profile_and_region_through():
    """Omitted --profile/--region fall through to boto3's own resolution."""
    ctor, session = _patched_session()
    with ctor as make_session:
        aws.s3_client(None, None)

    make_session.assert_called_once_with(profile_name=None)
    _, kwargs = session.client.call_args
    assert kwargs["region_name"] is None
    assert kwargs["config"] is None


def test_cloudformation_client_threads_profile_and_region():
    ctor, session = _patched_session()
    with ctor as make_session:
        client = aws.cloudformation_client("myprofile", "eu-west-1")

    make_session.assert_called_once_with(profile_name="myprofile")
    args, kwargs = session.client.call_args
    assert args[0] == "cloudformation"
    assert kwargs["region_name"] == "eu-west-1"
    assert client is session.client.return_value
