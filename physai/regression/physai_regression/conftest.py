"""Shared fixtures for regression checks.

These fixtures connect a live ``PhysaiClusterStack`` deployment to the
checks under ``physai_regression.checks``. The full chain is:

    pytest CLI flags  ──►  aws_profile / aws_region (None if unset; aws CLI then
                            uses its own profile/region resolution)
    aws cloudformation describe-stacks  ──►  cluster_name
    infra/scripts/setup-ssh.sh --output  ──►  ssh_config_path (tempfile)
    physai.ssh.Session(host, ssh_config=...)  ──►  physai_session

The user's ``~/.ssh/config`` is intentionally not touched — every SSH call
the checks make threads ``-F <ssh_config_path>`` via ``Session``.

Cluster connectivity (CFN describe, setup-ssh, SSH ControlMaster) only
happens when a check that requests one of these fixtures actually runs;
unit tests under ``regression/tests/`` that don't touch AWS are
unaffected.
"""

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from physai.ssh import Session

from .orchestration.deploy import (
    StackNotFound,
    StackQueryError,
    aws_cli_args,
    describe_stack,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SSH = REPO_ROOT / "infra" / "scripts" / "setup-ssh.sh"
SSH_HOST = "physai-login"
STACK_NAME = "PhysaiClusterStack"
INFRA_STACK_NAME = "PhysaiInfraStack"
FAKE_PROJECT_DIR = REPO_ROOT / "regression" / "fixtures" / "fake-project"
FAKE_RAW_DIR = REPO_ROOT / "regression" / "fixtures" / "fake-raw"


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--profile", action="store", default=None, help="AWS profile")
    parser.addoption("--region", action="store", default=None, help="AWS region")
    parser.addoption(
        "--cluster",
        action="store",
        default=None,
        help=f"Cluster name (default: resolved from {STACK_NAME} CFN output)",
    )
    parser.addoption(
        "--raw-source",
        action="store",
        default=None,
        help="URI to stage as the Layer 2 raw fixture "
        "(file:///abs/path | s3://bucket[/prefix/] | hf://owner/repo[@rev]). "
        "Required by Layer 2 checks; ignored by Layer 1.",
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: Sequence[pytest.Item]
) -> None:
    """Fail fast when Layer 2 checks are selected without ``--raw-source``.

    The runner (``python -m physai_regression --builtin-examples``)
    rejects the missing flag at argparse time, before any cluster deploy.
    This is the backstop for a direct ``pytest`` invocation: it errors at
    *collection* time — before any test runs — rather than letting the
    failure surface from the Layer 2 staging fixture after Layer 1 has
    already executed.

    ``trylast=True`` makes this run after pytest's own ``-m`` deselection,
    so ``items`` reflects what will actually run. Without it the default
    ``-m "not builtin_example"`` selector (``pytest.ini``) hasn't been
    applied yet and ``items`` still holds the Layer 2 test, which would
    abort an ordinary Layer 1 run that never intended to stage a fixture.
    """
    if config.getoption("--raw-source") is not None:
        return
    if any(item.get_closest_marker("builtin_example") for item in items):
        raise pytest.UsageError(
            "Layer 2 (builtin_example) checks require --raw-source <URI> "
            "(file:///abs/path | s3://bucket[/prefix/] | hf://owner/repo[@rev])"
        )


@pytest.fixture(scope="session")
def aws_profile(request: pytest.FixtureRequest) -> str | None:
    return request.config.getoption("--profile")


@pytest.fixture(scope="session")
def aws_region(request: pytest.FixtureRequest) -> str | None:
    return request.config.getoption("--region")


@pytest.fixture(scope="session")
def raw_source_uri(request: pytest.FixtureRequest) -> str | None:
    """Value of ``--raw-source`` for the Layer 2 staging fixture.

    Returns ``None`` for Layer 1 runs that don't supply the flag; the
    Layer 2 fixture turns the ``None`` into a hard test failure (the
    runner-level argparse normally enforces this, but a direct
    ``pytest`` invocation can reach the fixture without going through
    the runner).
    """
    return request.config.getoption("--raw-source")


def _stack_output(
    stack: str, output_key: str, profile: str | None, region: str | None
) -> str:
    """Read a single named output from a CFN stack, ``pytest.fail`` on absence."""
    try:
        stack_desc = describe_stack(stack, profile=profile, region=region)
    except (StackNotFound, StackQueryError) as e:
        # Both a missing stack and an unreachable CloudFormation should fail
        # the check cleanly — the message distinguishes which.
        pytest.fail(str(e))
    value = ""
    for output in stack_desc.get("Outputs", []) or []:
        if output.get("OutputKey") == output_key:
            value = output.get("OutputValue") or ""
            break
    if not value:
        pytest.fail(f"{stack} CFN output {output_key!r} is empty or missing")
    return value


@pytest.fixture(scope="session")
def cluster_name(
    request: pytest.FixtureRequest,
    aws_profile: str | None,
    aws_region: str | None,
) -> str:
    override = request.config.getoption("--cluster")
    if override:
        return override
    return _stack_output(STACK_NAME, "ClusterName", aws_profile, aws_region)


@pytest.fixture(scope="session")
def ssh_config_path(
    cluster_name: str,
    aws_profile: str | None,
    aws_region: str | None,
) -> Iterator[Path]:
    """Tempfile populated by ``setup-ssh.sh --output``; cleaned up on session end."""
    if not SETUP_SSH.is_file():
        pytest.fail(f"setup-ssh.sh not found at {SETUP_SSH}")
    fd, path_str = tempfile.mkstemp(prefix="physai-regression-ssh-", suffix=".config")
    os.close(fd)
    path = Path(path_str)
    cmd = [
        str(SETUP_SSH),
        "--cluster",
        cluster_name,
        "--output",
        str(path),
        *aws_cli_args(aws_profile, aws_region),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        path.unlink(missing_ok=True)
        pytest.fail(
            f"setup-ssh.sh --output failed (exit {r.returncode}).\n"
            f"stdout: {r.stdout.strip()}\nstderr: {r.stderr.strip()}"
        )
    yield path
    path.unlink(missing_ok=True)


@pytest.fixture(scope="session")
def physai_session(ssh_config_path: Path) -> Iterator[Session]:
    """A ``physai.ssh.Session`` to the login node, scoped to the test session."""
    if not shutil.which("ssh"):
        pytest.fail("`ssh` not found on PATH")
    s = Session(SSH_HOST, ssh_config=str(ssh_config_path))
    yield s
    s.close()


@pytest.fixture(scope="session")
def data_bucket_name(aws_profile: str | None, aws_region: str | None) -> str:
    """Resolve the S3 data bucket name from ``PhysaiInfraStack`` CFN output."""
    return _stack_output(INFRA_STACK_NAME, "DataBucketName", aws_profile, aws_region)


@pytest.fixture(scope="session")
def physai_cli(
    ssh_config_path: Path,
    aws_profile: str | None,
    aws_region: str | None,
) -> "PhysaiCLI":
    """A callable that invokes ``physai`` with ``--ssh-config`` pointed at the
    regression's tempfile config so it never touches ``~/.ssh/config``.

    Returns a :class:`PhysaiCLI` whose ``run`` method takes positional argv
    elements and forwards them. Each call subprocesses a fresh
    ``physai`` invocation; the CLI starts its own ControlMaster connection
    each time, but they all share the SSM tunnel.
    """
    if not shutil.which("physai"):
        pytest.fail("`physai` not found on PATH — install with `pip install -e cli`.")
    return PhysaiCLI(
        ssh_config=str(ssh_config_path),
        aws_profile=aws_profile,
        aws_region=aws_region,
    )


@pytest.fixture(scope="session")
def fake_project_dir() -> Path:
    """Local path to ``regression/fixtures/fake-project/``.

    Used by build/pipeline checks that need to point ``physai build`` /
    ``physai run`` at the fixture project on the developer's machine.
    """
    if not FAKE_PROJECT_DIR.is_dir():
        pytest.fail(f"fake-project fixture missing at {FAKE_PROJECT_DIR}")
    return FAKE_PROJECT_DIR


@pytest.fixture(scope="session")
def fake_raw_dir() -> Path:
    """Local path to ``regression/fixtures/fake-raw/``."""
    if not FAKE_RAW_DIR.is_dir():
        pytest.fail(f"fake-raw fixture missing at {FAKE_RAW_DIR}")
    return FAKE_RAW_DIR


@pytest.fixture(scope="session")
def fake_containers_built(physai_cli, fake_project_dir) -> None:
    """Build all three fake containers once per session.

    Several Layer 1 checks need the fake-project ``.sqsh`` images on
    ``/fsx/enroot/``. Building once at session start (with ``--rebuild``
    so the suite is robust to leftover state) shares the cost across
    every pipeline / visual / build check that depends on it.
    """
    for name in ("fake-converter", "fake-trainer", "fake-evaluator"):
        physai_cli.run(
            "build",
            str(fake_project_dir / "containers" / name),
            "--rebuild",
            "-n",
        )


class PhysaiCLI:
    """Subprocess wrapper for the ``physai`` CLI used by regression checks.

    Each invocation passes ``--ssh-config <path>`` as the first arg so the
    CLI uses the regression's tempfile SSH config (not ``~/.ssh/config``).
    """

    def __init__(
        self,
        ssh_config: str,
        aws_profile: str | None,
        aws_region: str | None,
    ):
        self.ssh_config = ssh_config
        self.aws_profile = aws_profile
        self.aws_region = aws_region

    def run(
        self,
        *args: str,
        check: bool = True,
        timeout: float | None = None,
        input: str | None = None,
    ) -> subprocess.CompletedProcess:
        """Run ``physai --host <SSH_HOST> --ssh-config <path> <args>...``.

        ``--host`` and ``--ssh-config`` together pin the CLI's connection
        target to the regression's tempfile config — overriding any
        ``host``/``ssh_config`` the user has in ``~/.physai/config.yaml``
        (e.g. ``physai-login2`` against a different cluster).

        ``check=True`` (the default) raises :class:`AssertionError` on
        non-zero exit so test failure messages report what failed cleanly.
        """
        cmd = [
            "physai",
            "--host",
            SSH_HOST,
            "--ssh-config",
            self.ssh_config,
            *args,
        ]
        env = {**os.environ}
        if self.aws_profile:
            env["AWS_PROFILE"] = self.aws_profile
        if self.aws_region:
            env["AWS_DEFAULT_REGION"] = self.aws_region
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=timeout,
            input=input,
        )
        if check and r.returncode != 0:
            raise AssertionError(
                f"`{' '.join(cmd)}` exited {r.returncode}.\n"
                f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
            )
        return r
