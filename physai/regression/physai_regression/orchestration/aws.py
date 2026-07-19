"""boto3 client factory for the laptop-side AWS calls in the regression suite.

The regression run threads the user's ``--profile`` / ``--region`` (from the
pytest CLI flags) through every AWS call it makes from the laptop. These
helpers centralize turning that ``(profile, region)`` pair into a boto3 client
so ``deploy`` (CloudFormation) and ``raw_staging`` (S3) build clients
identically. ``profile=None`` / ``region=None`` fall through to boto3's own
resolution (env vars, shared config), matching the CLI's behavior when the
flags are omitted.

Cluster-side ``aws`` invocations (run over SSH via ``session.run``) and the
``cdk`` calls stay on the CLI and do NOT use this module.
"""

import boto3
from botocore.client import BaseClient
from botocore.config import Config

# Presigned URLs must be region-correct: a URL signed for the wrong region
# gets a PermanentRedirect from S3, and ``curl`` then writes the redirect's
# XML body into the output file. A default client emits a legacy SigV2 URL
# that binds no region at all (even when the resolved signature_version
# claims s3v4); forcing SigV4 + virtual-hosted addressing puts the region in
# both the host and the credential scope, so the signature matches the
# bucket's home region.
_PRESIGN_CONFIG = Config(signature_version="s3v4", s3={"addressing_style": "virtual"})


def _session(profile: str | None) -> boto3.session.Session:
    return boto3.session.Session(profile_name=profile)


def s3_client(
    profile: str | None, region: str | None, *, for_presign: bool = False
) -> BaseClient:
    """Build an S3 client bound to ``region`` (used as the signing region).

    ``for_presign=True`` pins SigV4 + virtual-hosted addressing so
    ``generate_presigned_url`` produces a region-correct URL.
    """
    config = _PRESIGN_CONFIG if for_presign else None
    return _session(profile).client("s3", region_name=region, config=config)


def cloudformation_client(profile: str | None, region: str | None) -> BaseClient:
    """Build a CloudFormation client (for ``describe_stacks`` etc.)."""
    return _session(profile).client("cloudformation", region_name=region)
