"""SSH session — multiplexed connection with cached metadata."""

import signal
import subprocess
import sys
import tempfile
from pathlib import Path

LOG_STREAMER = Path(__file__).parent / "log_streamer.py"


class Session:
    """A multiplexed SSH session to a remote host."""

    def __init__(self, host: str):
        self.host = host
        self._tmpdir = tempfile.mkdtemp(prefix="physai-ssh-")
        self._socket = f"{self._tmpdir}/ctrl"
        self._cache: dict = {}
        # Start ControlMaster
        r = subprocess.run(
            ["ssh", "-fNM", "-S", self._socket, "-o", "ControlPersist=10m", host],
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

    def _ssh_args(self) -> list[str]:
        return ["ssh", "-S", self._socket, "-o", "ControlMaster=no", self.host]

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

        With show_progress=True, rsync streams a live one-line progress update
        to the terminal (`--info=progress2`) and its stderr is not captured.
        Use this for large uploads where silent waits are a bad UX. For small,
        quick rsyncs (scripts, configs) leave it off so logs stay clean.
        """
        cmd = [
            "rsync",
            "-az",
            "-e",
            f"ssh -S {self._socket} -o ControlMaster=no",
        ]
        if show_progress:
            cmd += ["--human-readable", "--info=progress2"]
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
        s._tmpdir = self._tmpdir
        s._socket = self._socket
        s._cache = dict(self._cache)
        return s

    def close(self) -> None:
        subprocess.run(
            ["ssh", "-S", self._socket, "-O", "exit", self.host],
            capture_output=True,
            check=False,
        )
