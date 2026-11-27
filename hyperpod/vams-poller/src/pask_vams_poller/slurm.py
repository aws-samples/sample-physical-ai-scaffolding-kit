"""Thin wrapper of the Slurm commands used by the poller: sbatch, squeue, and scancel."""

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

TRANSIENT_STDERR_MARKERS: tuple[str, ...] = (
    "Socket timed out",
    "Unable to contact slurm controller",
    "Resource temporarily unavailable",
    "Slurm temporarily unable",
)
SCANCEL_NO_JOB_MARKER = "Invalid job id"
SQUEUE_FORMAT = "%i|%T"


@dataclass
class SqueueJob:
    """One line of ``squeue`` output."""

    job_id: str
    state: str


class SlurmCommandError(Exception):
    """A Slurm command failed.

    Attributes:
        returncode: Exit status, or None when the command timed out.
        stderr: Standard error of the command.
        transient: True when the controller was temporarily unavailable.
    """

    def __init__(self, command: str, returncode: int | None, stderr: str) -> None:
        """Create the error and classify it.

        Args:
            command: Name of the command, such as ``sbatch``.
            returncode: Exit status, or None when the command timed out.
            stderr: Standard error of the command.
        """
        super().__init__(f"{command} failed with exit status {returncode}: {stderr.strip()}")
        self.returncode = returncode
        self.stderr = stderr
        self.transient = returncode is None or is_transient_stderr(stderr)


def is_transient_stderr(stderr: str) -> bool:
    """Tell whether Slurm's error output means the controller is temporarily unavailable.

    Args:
        stderr: Standard error of a Slurm command.

    Returns:
        True when it contains one of ``TRANSIENT_STDERR_MARKERS``.
    """
    return any(marker in stderr for marker in TRANSIENT_STDERR_MARKERS)


class SlurmClient:
    """Run Slurm commands without a shell, passing arguments as a list."""

    def __init__(
        self,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        *,
        sbatch: str = "sbatch",
        squeue: str = "squeue",
        scancel: str = "scancel",
        timeout: float = 60,
    ) -> None:
        """Create the client.

        Args:
            runner: Function with the signature of ``subprocess.run``.
            sbatch: sbatch executable.
            squeue: squeue executable.
            scancel: scancel executable.
            timeout: Seconds to wait for each command.
        """
        self._runner = runner
        self._sbatch = sbatch
        self._squeue = squeue
        self._scancel = scancel
        self._timeout = timeout

    def submit(
        self,
        *,
        job_name: str,
        script: str,
        script_args: list[str],
        chdir: str,
        output: str,
        error: str,
        env: dict[str, str],
        partition: str | None,
    ) -> str:
        """Submit a batch job that does not requeue.

        The job environment is passed only through ``env`` and exported with
        ``--export=ALL``, so no value of ``env`` appears on the command line.

        Args:
            job_name: Slurm job name.
            script: Batch script path, relative to ``chdir`` or absolute.
            script_args: Arguments of the script.
            chdir: Working directory of the job.
            output: ``--output`` path. ``%j`` is replaced by the job id.
            error: ``--error`` path. ``%j`` is replaced by the job id.
            env: Complete environment of the sbatch process, including ``PATH``.
            partition: Slurm partition, or None for the cluster default.

        Returns:
            The job id.

        Raises:
            SlurmCommandError: sbatch failed or printed no job id.
        """
        args = [
            self._sbatch,
            "--parsable",
            "--no-requeue",
            f"--job-name={job_name}",
            f"--chdir={chdir}",
            f"--output={output}",
            f"--error={error}",
            "--export=ALL",
        ]
        if partition:
            args.append(f"--partition={partition}")
        args.extend([script, *script_args])
        stdout = self._run("sbatch", args, env=env)
        job_id = stdout.strip().split(";", 1)[0]
        if not job_id:
            raise SlurmCommandError("sbatch", 0, "sbatch printed no job id")
        return job_id

    def find_by_name(self, job_name: str) -> list[SqueueJob]:
        """List the jobs in the queue with a given name.

        Args:
            job_name: Slurm job name.

        Returns:
            The jobs. Empty when none is queued or running.

        Raises:
            SlurmCommandError: squeue failed.
        """
        args = [self._squeue, "--noheader", f"--name={job_name}", f"--format={SQUEUE_FORMAT}"]
        stdout = self._run("squeue", args, env=None)
        return [_parse_squeue_line(line) for line in stdout.splitlines() if line.strip()]

    def cancel_by_name(self, job_name: str) -> None:
        """Cancel the jobs with a given name. No matching job counts as success.

        Args:
            job_name: Slurm job name.

        Raises:
            SlurmCommandError: scancel failed for another reason.
        """
        args = [self._scancel, f"--name={job_name}"]
        try:
            self._run("scancel", args, env=None)
        except SlurmCommandError as error:
            if SCANCEL_NO_JOB_MARKER not in error.stderr:
                raise

    def _run(self, command: str, args: list[str], env: dict[str, str] | None) -> str:
        """Run one Slurm command.

        Args:
            command: Name of the command, used in errors.
            args: Full argument list.
            env: Environment of the process, or None to inherit the poller's environment.

        Returns:
            Standard output.

        Raises:
            SlurmCommandError: The command timed out or exited with a non-zero status.
        """
        run_kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": self._timeout,
            "check": False,
        }
        if env is not None:
            run_kwargs["env"] = env
        try:
            completed = self._runner(args, **run_kwargs)
        except subprocess.TimeoutExpired as error:
            raise SlurmCommandError(command, None, f"timed out after {error.timeout}s") from error
        if completed.returncode != 0:
            raise SlurmCommandError(command, completed.returncode, completed.stderr or "")
        return completed.stdout or ""


def _parse_squeue_line(line: str) -> SqueueJob:
    """Parse one ``%i|%T`` line of squeue output.

    Args:
        line: The line.

    Returns:
        The job.

    Raises:
        SlurmCommandError: The line does not have the expected form.
    """
    job_id, separator, state = line.strip().partition("|")
    if not separator or not job_id or not state:
        raise SlurmCommandError("squeue", 0, f"unexpected squeue line: {line!r}")
    return SqueueJob(job_id=job_id, state=state)
