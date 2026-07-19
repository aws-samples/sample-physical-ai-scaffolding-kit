"""Wrappers for ``cdk`` and CloudFormation operations.

Each primitive shells out via ``subprocess.run`` and is mockable at the
``subprocess.run`` boundary. The CDK app lives at ``physai/infra/``; each
primitive accepts ``infra_dir`` so the modes can override (e.g. the
``upgrade-from-ref`` mode uses a worktree path).

Region handling: ``cdk deploy`` and ``cdk destroy`` silently accept
``--region`` but do not honor it (see aws/aws-cdk#28725 — the flag is
listed by the top-level ``cdk`` parser but not by ``deploy``/``destroy``,
so it gets parsed and discarded). For environment-agnostic stacks like
this app's, the deploy region is resolved from ``AWS_REGION`` /
``AWS_DEFAULT_REGION`` in the subprocess env (which CDK exports as
``CDK_DEFAULT_REGION`` to the synth subprocess). These helpers therefore
inject the region via env, not argv. ``--profile`` is documented and
honored, so it stays as a flag.
"""

import os
import subprocess
from pathlib import Path

from botocore.exceptions import ClientError

from .aws import cloudformation_client

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_INFRA_DIR = REPO_ROOT / "infra"
RUN_LIFECYCLE_SH = REPO_ROOT / "infra" / "scripts" / "run-lifecycle.sh"
CLUSTER_STACK = "PhysaiClusterStack"


def aws_cli_args(
    profile: str | None,
    region: str | None,
    *,
    include_region: bool = True,
) -> list[str]:
    """Build ``--profile``/``--region`` argv suffixes for an ``aws`` CLI call,
    skipping each flag when its value is None.

    ``include_region=False`` omits ``--region`` even when ``region`` is set —
    used for ``cdk`` invocations, which silently ignore ``--region`` and get
    the region from ``AWS_REGION``/``AWS_DEFAULT_REGION`` in the subprocess env
    instead (see module docstring; aws/aws-cdk#28725)."""
    args: list[str] = []
    if profile:
        args += ["--profile", profile]
    if include_region and region:
        args += ["--region", region]
    return args


class StackNotFound(RuntimeError):
    """The named stack genuinely does not exist in this account/region.

    Raised only when ``describe_stacks`` fails with CloudFormation's
    "does not exist" ``ValidationError`` — never for auth, network, or
    other failures, which raise :class:`StackQueryError` instead so they
    can't be mistaken for absence.
    """


class StackQueryError(RuntimeError):
    """``describe-stacks`` failed for a reason other than the stack being
    absent (expired/insufficient credentials, throttling, wrong region,
    no network). Callers must NOT treat this as "stack absent"."""


# CloudFormation's ``describe_stacks`` reports a genuinely-missing stack as a
# ``ClientError`` with ``Code == "ValidationError"`` and a message like
# "Stack with id X does not exist". ``ValidationError`` is generic (a
# malformed request also raises it), so the message substring is still needed
# to distinguish absence from other validation failures.
_STACK_ABSENT_SIGNATURE = "does not exist"


def describe_stack(
    stack: str,
    profile: str | None = None,
    region: str | None = None,
) -> dict:
    """Return the CloudFormation description dict for ``stack`` (``Stacks[0]``).

    Calls ``cloudformation.describe_stacks(StackName=stack)`` via boto3 and
    returns the first (and only) stack's dict, so callers navigate its
    ``StackStatus`` / ``Outputs`` themselves. Raises :class:`StackNotFound`
    when the stack genuinely does not exist, and :class:`StackQueryError` for
    any other failure (auth, network, throttling, wrong region) — the
    distinction matters because :func:`stack_exists` maps only the former to
    "absent".
    """
    client = cloudformation_client(profile, region)
    try:
        resp = client.describe_stacks(StackName=stack)
    except ClientError as e:
        err = e.response.get("Error", {})
        code = err.get("Code", "")
        message = err.get("Message", "")
        prefix = f"describe-stacks --stack-name {stack} failed: "
        if code == "ValidationError" and _STACK_ABSENT_SIGNATURE in message:
            raise StackNotFound(prefix + message) from e
        raise StackQueryError(prefix + f"({code}) {message}") from e
    stacks = resp.get("Stacks", [])
    if not stacks:
        # describe-stacks by name returns exactly one stack or raises
        # ValidationError; an empty list would be an API contract break.
        raise StackNotFound(f"describe-stacks --stack-name {stack} returned no stacks")
    return stacks[0]


def _cdk_env(region: str | None) -> dict[str, str]:
    """Subprocess env for ``cdk`` invocations.

    Pins ``AWS_REGION`` / ``AWS_DEFAULT_REGION`` to the requested region
    so the synth subprocess resolves env-agnostic stacks to the right
    region regardless of the parent shell's settings.
    """
    env = dict(os.environ)
    if region:
        env["AWS_REGION"] = region
        env["AWS_DEFAULT_REGION"] = region
    return env


def stack_exists(
    stack: str,
    profile: str | None = None,
    region: str | None = None,
) -> bool:
    """Return True when the stack is present in CloudFormation.

    A stack in ``DELETE_COMPLETE`` is reported as absent: ``describe-stacks``
    only surfaces it when filtered by name, and treating it as present would
    make the fresh mode attempt a destroy that does nothing useful.

    Only a genuine :class:`StackNotFound` maps to ``False``. A
    :class:`StackQueryError` (auth/network/throttle) propagates: a caller
    like ``cdk_destroy(skip_if_absent=True)`` must not silently skip the
    destroy — and leak a live cluster — just because the existence probe
    couldn't reach CloudFormation.
    """
    try:
        stack_desc = describe_stack(stack, profile=profile, region=region)
    except StackNotFound:
        return False
    status = stack_desc.get("StackStatus", "")
    return bool(status) and status != "DELETE_COMPLETE"


def cdk_deploy(
    stack: str = CLUSTER_STACK,
    profile: str | None = None,
    region: str | None = None,
    infra_dir: Path | None = None,
) -> None:
    """Run ``npx cdk deploy <stack> --require-approval never``.

    Streams stdout/stderr to the parent process so the deploy progress is
    visible while the regression run is in flight. ``region`` is passed
    via env vars (see module docstring), not as a flag.
    """
    cwd = infra_dir or DEFAULT_INFRA_DIR
    cmd = [
        "npx",
        "cdk",
        "deploy",
        stack,
        "--require-approval",
        "never",
        *aws_cli_args(profile, region, include_region=False),
    ]
    r = subprocess.run(cmd, cwd=str(cwd), env=_cdk_env(region), check=False)
    if r.returncode != 0:
        raise RuntimeError(f"cdk deploy {stack} failed (exit {r.returncode})")


def cdk_destroy(
    stack: str = CLUSTER_STACK,
    profile: str | None = None,
    region: str | None = None,
    infra_dir: Path | None = None,
    skip_if_absent: bool = True,
) -> None:
    """Run ``npx cdk destroy <stack> --force``.

    With ``skip_if_absent=True`` (the default), checks CloudFormation first
    and returns without invoking ``cdk`` if the stack is not present.
    ``region`` is passed via env vars (see module docstring), not as a flag.
    """
    if skip_if_absent and not stack_exists(stack, profile=profile, region=region):
        return
    cwd = infra_dir or DEFAULT_INFRA_DIR
    cmd = [
        "npx",
        "cdk",
        "destroy",
        stack,
        "--force",
        *aws_cli_args(profile, region, include_region=False),
    ]
    r = subprocess.run(cmd, cwd=str(cwd), env=_cdk_env(region), check=False)
    if r.returncode != 0:
        raise RuntimeError(f"cdk destroy {stack} failed (exit {r.returncode})")


def npm_ci(infra_dir: Path) -> None:
    """Install ``infra/`` npm dependencies via ``npm ci``.

    ``npm ci`` is required because a fresh worktree has no ``node_modules/``
    — the parent worktree's install does not carry over. Without this step,
    ``cdk deploy`` fails to resolve ``aws-cdk-lib`` and ts-node falls back
    to compiling against missing types.
    """
    cmd = ["npm", "ci"]
    r = subprocess.run(cmd, cwd=str(infra_dir), check=False)
    if r.returncode != 0:
        raise RuntimeError(f"npm ci failed in {infra_dir} (exit {r.returncode})")


def run_lifecycle_all(
    profile: str | None = None,
    region: str | None = None,
) -> None:
    """Run ``infra/scripts/run-lifecycle.sh --all`` against the live cluster.

    Re-applies the on-disk ``infra/lifecycle/`` scripts to every node via
    SSM. Pairs with :func:`cdk_deploy` in the upgrade flow: ``cdk deploy``
    re-uploads the scripts to S3 so future node replacements use the new
    version, and ``--all`` applies them to nodes that are already running.

    The script handles its own per-node logging under
    ``/tmp/physai-lifecycle-runs/<timestamp>/``; stdout/stderr stream to the
    parent so the user sees the per-node summary in real time.
    """
    if not RUN_LIFECYCLE_SH.is_file():
        raise RuntimeError(f"run-lifecycle.sh not found at {RUN_LIFECYCLE_SH}")
    cmd = [str(RUN_LIFECYCLE_SH), "--all", *aws_cli_args(profile, region)]
    r = subprocess.run(cmd, cwd=str(DEFAULT_INFRA_DIR), check=False)
    if r.returncode != 0:
        raise RuntimeError(f"run-lifecycle.sh --all failed (exit {r.returncode})")
