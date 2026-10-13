"""SSH session — multiplexed connection with cached metadata."""

import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

LOG_STREAMER = Path(__file__).parent / "log_streamer.py"


def _rsync_supports_progress2() -> bool:
    """Whether the local rsync understands `--info=progress2` (added in rsync 3.1.0).

    macOS ships rsync 2.6.9 / openrsync, which only support the older
    `--progress`. We parse `rsync --version` and compare; on any doubt we
    return False and let the caller fall back to the universally-supported
    `--progress`.
    """
    try:
        out = subprocess.run(
            ["rsync", "--version"], capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        return False
    # First "<major>.<minor>" after the word "version". openrsync prints
    # "protocol version 29" first (no dot, skipped) then "rsync version 2.6.9
    # compatible" — so this resolves to 2.6 for openrsync, < 3.1 as intended.
    m = re.search(r"\bversion (\d+)\.(\d+)", out)
    if not m:
        return False
    return (int(m.group(1)), int(m.group(2))) >= (3, 1)


class Session:
    """A multiplexed SSH session to a remote host."""

    def __init__(self, host: str, ssh_config: str | None = None):
        self.host = host
        self.ssh_config = ssh_config
        self._tmpdir = tempfile.mkdtemp(prefix="physai-ssh-")
        self._socket = f"{self._tmpdir}/ctrl"
        self._cache: dict = {}
        # Start ControlMaster
        r = subprocess.run(
            [
                "ssh",
                *self._config_args(),
                "-fNM",
                "-S",
                self._socket,
                "-o",
                "ControlPersist=10m",
                host,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            stderr = r.stderr.strip()
            raise SystemExit(
                f"{stderr}\n"
                f"{'-' * 60}\n"
                f"Failed to connect to '{host}' via SSH.\n"
                f"Test the connection manually: ssh {host}\n"
                f"Common causes: host key mismatch (~/.ssh/known_hosts), "
                f"missing entry in ~/.ssh/config, or expired credentials."
            )

    def _config_args(self) -> list[str]:
        """`-F <ssh_config>` if a config file is set, else nothing."""
        return ["-F", self.ssh_config] if self.ssh_config else []

    def _ssh_args(self) -> list[str]:
        return [
            "ssh",
            *self._config_args(),
            "-S",
            self._socket,
            "-o",
            "ControlMaster=no",
            self.host,
        ]

    def _rsync_ssh_arg(self) -> str:
        """The `ssh ...` command rsync invokes via `-e`."""
        parts = ["ssh"]
        if self.ssh_config:
            parts += ["-F", self.ssh_config]
        parts += ["-S", self._socket, "-o", "ControlMaster=no"]
        return " ".join(parts)

    def run(self, cmd: str) -> str:
        """Run a command and return stdout."""
        r = subprocess.run(
            self._ssh_args() + [cmd], capture_output=True, text=True, check=False
        )
        if r.returncode != 0:
            raise RuntimeError(f"ssh {self.host}: {r.stderr.strip()}")
        return r.stdout.strip()

    def rsync(self, src: str, dst: str, show_progress: bool = False) -> None:
        """rsync a local path to the remote host.

        With show_progress=True, rsync streams a live progress update to the
        terminal and its stderr is not captured. Use this for large uploads
        where silent waits are a bad UX. For small, quick rsyncs (scripts,
        configs) leave it off so logs stay clean.

        Progress style depends on the local rsync: 3.1.0+ gets the aggregate
        one-line `--info=progress2` bar; older rsync / openrsync (e.g. macOS's
        2.6.9) falls back to per-file `--progress`, which every rsync supports.
        """
        cmd = [
            "rsync",
            "-az",
            "-e",
            self._rsync_ssh_arg(),
        ]
        if show_progress:
            if _rsync_supports_progress2():
                cmd += ["--human-readable", "--info=progress2"]
            else:
                cmd += ["--progress"]
        cmd += [str(src), f"{self.host}:{dst}"]

        if show_progress:
            # Inherit stdout/stderr so rsync's \r-based progress line is
            # rendered live in the user's terminal.
            r = subprocess.run(cmd, check=False)
            if r.returncode != 0:
                raise RuntimeError(
                    f"rsync to {self.host}:{dst} failed (exit {r.returncode})"
                )
        else:
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if r.returncode != 0:
                raise RuntimeError(f"rsync to {self.host}:{dst}: {r.stderr.strip()}")

    def run_streaming(self, cmd: str) -> None:
        r"""Run a remote command with stdout/stderr inherited from the caller.

        Use this for commands whose *progress* the user needs to see live —
        long-running transfers like `aws s3 cp --recursive` that emit their
        own \r-based progress lines. `run()` captures output and only returns
        it at the end, which turns a 20-minute copy into a silent black box.
        """
        r = subprocess.run(self._ssh_args() + [cmd], check=False)
        if r.returncode != 0:
            raise RuntimeError(
                f"ssh {self.host}: command failed (exit {r.returncode}): {cmd}"
            )

    def write_file(self, path: str, content: str) -> None:
        """Write content to a file on the remote host."""
        r = subprocess.run(
            self._ssh_args() + [f"cat > {path}"],
            input=content,
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(f"write {self.host}:{path}: {r.stderr.strip()}")

    def stream_log(self, job_id: str) -> None:
        """Stream a job's log via the remote helper script. Ctrl-C detaches cleanly."""
        with open(LOG_STREAMER) as f:
            proc = subprocess.Popen(
                self._ssh_args() + [f"python3 - {job_id}"],
                stdin=f,
            )
            try:
                proc.wait()
            except KeyboardInterrupt:
                proc.send_signal(signal.SIGTERM)
                proc.wait()
                print(
                    f"\nDetached. Job may still be running. Reconnect: physai logs {job_id}"
                )
                sys.exit(0)

    @property
    def has_sacct(self) -> bool:
        if "has_sacct" not in self._cache:
            try:
                self.run("sacct -n --parsable2 -S now-1hour -o JobID")
                self._cache["has_sacct"] = True
            except RuntimeError:
                self._cache["has_sacct"] = False
        return self._cache["has_sacct"]

    def clone(self) -> "Session":
        """Create a new session reusing the same ControlMaster connection."""
        s = object.__new__(Session)
        s.host = self.host
        s.ssh_config = self.ssh_config
        s._tmpdir = self._tmpdir
        s._socket = self._socket
        s._cache = dict(self._cache)
        return s

    def close(self) -> None:
        subprocess.run(
            ["ssh", *self._config_args(), "-S", self._socket, "-O", "exit", self.host],
            capture_output=True,
            check=False,
        )
