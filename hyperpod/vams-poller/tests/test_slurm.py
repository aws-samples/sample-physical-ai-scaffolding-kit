"""Tests for the Slurm command wrapper."""

import subprocess
from dataclasses import dataclass, field
from typing import Any

import pytest

from pask_vams_poller.slurm import SlurmClient, SlurmCommandError, SqueueJob


@dataclass
class FakeRunner:
    """Stand-in for ``subprocess.run`` that records calls and returns canned results."""

    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    raise_timeout: bool = False
    calls: list[tuple[list[str], dict[str, Any]]] = field(default_factory=list)

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Record the call and return the canned result."""
        self.calls.append((args, kwargs))
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.CompletedProcess(args, self.returncode, self.stdout, self.stderr)


def _submit(client: SlurmClient, env: dict[str, str], partition: str | None) -> str:
    """Submit the VAMS training job shape used by the poller."""
    return client.submit(
        job_name="vams-pexec1",
        script="slurm_train.sh",
        script_args=["vams-pexec1"],
        chdir="/fsx/ubuntu/lerobot-openpi/training",
        output="/fsx/ubuntu/lerobot-openpi/vams-runs/pexec1/slurm_%j.out",
        error="/fsx/ubuntu/lerobot-openpi/vams-runs/pexec1/slurm_%j.err",
        env=env,
        partition=partition,
    )


def test_submit_builds_argv_and_passes_env_separately() -> None:
    runner = FakeRunner(stdout="1234;pask-cluster\n")
    env = {"PATH": "/usr/bin", "HF_TOKEN": "hf_secret_value", "STEPS": "3000"}

    job_id = _submit(SlurmClient(runner, timeout=30), env, partition="gpu")

    assert job_id == "1234"
    args, kwargs = runner.calls[0]
    assert args == [
        "sbatch",
        "--parsable",
        "--no-requeue",
        "--job-name=vams-pexec1",
        "--chdir=/fsx/ubuntu/lerobot-openpi/training",
        "--output=/fsx/ubuntu/lerobot-openpi/vams-runs/pexec1/slurm_%j.out",
        "--error=/fsx/ubuntu/lerobot-openpi/vams-runs/pexec1/slurm_%j.err",
        "--export=ALL",
        "--partition=gpu",
        "slurm_train.sh",
        "vams-pexec1",
    ]
    assert kwargs["env"] == env
    assert kwargs["timeout"] == 30
    assert "shell" not in kwargs
    assert not any("hf_secret_value" in arg or "3000" in arg for arg in args)


@pytest.mark.parametrize("partition", [None, ""])
def test_submit_without_partition_omits_flag(partition: str | None) -> None:
    runner = FakeRunner(stdout="99\n")

    assert _submit(SlurmClient(runner), {"PATH": "/usr/bin"}, partition) == "99"
    args, _ = runner.calls[0]
    assert not any(arg.startswith("--partition") for arg in args)


def test_submit_failure_raises_with_stderr() -> None:
    runner = FakeRunner(returncode=1, stderr="sbatch: error: Batch job submission failed")

    with pytest.raises(SlurmCommandError) as failure:
        _submit(SlurmClient(runner), {}, None)

    assert failure.value.returncode == 1
    assert failure.value.transient is False


def test_submit_without_job_id_raises() -> None:
    with pytest.raises(SlurmCommandError):
        _submit(SlurmClient(FakeRunner(stdout="\n")), {}, None)


def test_find_by_name_parses_jobs() -> None:
    runner = FakeRunner(stdout="1234|RUNNING\n1235|PENDING\n")

    jobs = SlurmClient(runner, squeue="/opt/slurm/bin/squeue").find_by_name("vams-pexec1")

    assert jobs == [SqueueJob("1234", "RUNNING"), SqueueJob("1235", "PENDING")]
    args, kwargs = runner.calls[0]
    assert args == [
        "/opt/slurm/bin/squeue",
        "--noheader",
        "--name=vams-pexec1",
        "--format=%i|%T",
    ]
    assert "env" not in kwargs


def test_find_by_name_empty_queue() -> None:
    assert SlurmClient(FakeRunner(stdout="")).find_by_name("vams-pexec1") == []


def test_cancel_by_name_builds_argv() -> None:
    runner = FakeRunner()

    SlurmClient(runner).cancel_by_name("vams-pexec1")

    assert runner.calls[0][0] == ["scancel", "--name=vams-pexec1"]


def test_cancel_by_name_without_matching_job_succeeds() -> None:
    runner = FakeRunner(returncode=1, stderr="scancel: error: Invalid job id specified")

    SlurmClient(runner).cancel_by_name("vams-pexec1")


def test_cancel_by_name_other_error_raises() -> None:
    runner = FakeRunner(returncode=1, stderr="scancel: error: Access/permission denied")

    with pytest.raises(SlurmCommandError):
        SlurmClient(runner).cancel_by_name("vams-pexec1")


@pytest.mark.parametrize(
    "stderr",
    [
        "squeue: error: Socket timed out on send/recv operation",
        "squeue: error: Unable to contact slurm controller (connect failure)",
        "sbatch: error: Resource temporarily unavailable",
        "sbatch: error: Slurm temporarily unable to accept job, sleeping and retrying",
    ],
)
def test_transient_stderr_is_classified(stderr: str) -> None:
    with pytest.raises(SlurmCommandError) as failure:
        SlurmClient(FakeRunner(returncode=1, stderr=stderr)).find_by_name("vams-pexec1")

    assert failure.value.transient is True
    assert failure.value.stderr == stderr


def test_timeout_is_transient() -> None:
    with pytest.raises(SlurmCommandError) as failure:
        SlurmClient(FakeRunner(raise_timeout=True), timeout=5).find_by_name("vams-pexec1")

    assert failure.value.returncode is None
    assert failure.value.transient is True
