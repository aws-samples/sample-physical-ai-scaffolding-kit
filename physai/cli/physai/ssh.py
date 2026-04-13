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
        subprocess.run(
            ["ssh", "-fNM", "-S", self._socket, "-o", "ControlPersist=10m", host],
            check=True,
            capture_output=True,
        )

    def _ssh_args(self) -> list[str]:
        return ["ssh", "-S", self._socket, "-o", "ControlMaster=no", self.host]

    def run(self, cmd: str) -> str:
        """Run a command and return stdout."""
        r = subprocess.run(self._ssh_args() + [cmd], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"ssh {self.host}: {r.stderr.strip()}")
        return r.stdout.strip()

    def rsync(self, src: str, dst: str) -> None:
        """rsync a local path to the remote host."""
        r = subprocess.run(
            [
                "rsync",
                "-az",
                "-e",
                f"ssh -S {self._socket} -o ControlMaster=no",
                str(src),
                f"{self.host}:{dst}",
            ],
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"rsync to {self.host}:{dst}: {r.stderr.strip()}")

    def write_file(self, path: str, content: str) -> None:
        """Write content to a file on the remote host."""
        r = subprocess.run(
            self._ssh_args() + [f"cat > {path}"],
            input=content,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"write {self.host}:{path}: {r.stderr.strip()}")

    def stream_log(self, job_id: str) -> None:
        """Stream a job's log via the remote helper script. Ctrl-C detaches cleanly."""
        proc = subprocess.Popen(
            self._ssh_args() + [f"python3 - {job_id}"],
            stdin=open(LOG_STREAMER),
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
                self.run("sacct -n --parsable2 -S now-1hour -e JobID")
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
        )
