"""Tests for `physai.ssh.Session`.

Focus: every subprocess invocation made by Session must respect the
`ssh_config` argument. When set, every `ssh` call gets `-F <path>` and every
`rsync -e 'ssh ...'` argument embeds `-F <path>` so the user's
`~/.ssh/config` is not required. The same wiring is reachable two ways
(config file and `--ssh-config` CLI override); the integration test below
exercises both.
"""

from unittest.mock import MagicMock, patch

import pytest

from physai import config as config_module
from physai.ssh import Session, _rsync_supports_progress2


@pytest.fixture
def fake_run():
    """Patch subprocess.run inside physai.ssh; return the mock."""
    with patch("physai.ssh.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        yield run


def make_session(fake_run, ssh_config: str | None = None) -> Session:
    """Build a Session, asserting the ControlMaster handshake succeeds."""
    s = Session("myhost", ssh_config=ssh_config)
    fake_run.reset_mock()
    return s


def _last_argv(fake_run) -> list[str]:
    """argv list passed to the most recent subprocess.run call."""
    args, _kwargs = fake_run.call_args
    return args[0]


# ── ControlMaster handshake ─────────────────────────────────────────────────


def test_init_handshake_without_ssh_config(fake_run):
    Session("myhost")
    argv = _last_argv(fake_run)
    assert argv[0] == "ssh"
    assert "-F" not in argv
    assert "-fNM" in argv


def test_init_handshake_with_ssh_config(fake_run):
    Session("myhost", ssh_config="/tmp/cfg")
    argv = _last_argv(fake_run)
    assert argv[0] == "ssh"
    assert argv[1:3] == ["-F", "/tmp/cfg"]
    assert "-fNM" in argv


# ── run / write_file / stream_log ───────────────────────────────────────────


def test_run_includes_ssh_config(fake_run):
    s = make_session(fake_run, ssh_config="/tmp/cfg")
    s.run("hostname")
    argv = _last_argv(fake_run)
    assert argv[0] == "ssh"
    assert argv[1:3] == ["-F", "/tmp/cfg"]
    assert argv[-2:] == ["myhost", "hostname"]


def test_run_omits_flag_without_ssh_config(fake_run):
    s = make_session(fake_run)
    s.run("hostname")
    argv = _last_argv(fake_run)
    assert "-F" not in argv


def test_write_file_includes_ssh_config(fake_run):
    s = make_session(fake_run, ssh_config="/tmp/cfg")
    s.write_file("/remote/path", "hello")
    argv = _last_argv(fake_run)
    assert "-F" in argv and argv[argv.index("-F") + 1] == "/tmp/cfg"


def test_close_includes_ssh_config(fake_run):
    s = make_session(fake_run, ssh_config="/tmp/cfg")
    s.close()
    argv = _last_argv(fake_run)
    assert argv[0] == "ssh"
    assert "-F" in argv and argv[argv.index("-F") + 1] == "/tmp/cfg"


# ── rsync ───────────────────────────────────────────────────────────────────


def test_rsync_embeds_ssh_config_in_dash_e(fake_run):
    s = make_session(fake_run, ssh_config="/tmp/cfg")
    s.rsync("/local/src", "/remote/dst")
    argv = _last_argv(fake_run)
    assert argv[0] == "rsync"
    e_idx = argv.index("-e")
    ssh_arg = argv[e_idx + 1]
    assert ssh_arg.startswith("ssh ")
    assert "-F /tmp/cfg" in ssh_arg


def test_rsync_omits_dash_F_without_ssh_config(fake_run):
    s = make_session(fake_run)
    s.rsync("/local/src", "/remote/dst")
    argv = _last_argv(fake_run)
    e_idx = argv.index("-e")
    assert "-F" not in argv[e_idx + 1]


def test_rsync_progress_path_also_uses_ssh_config(fake_run):
    s = make_session(fake_run, ssh_config="/tmp/cfg")
    with patch("physai.ssh._rsync_supports_progress2", return_value=True):
        s.rsync("/src", "/dst", show_progress=True)
    argv = _last_argv(fake_run)
    assert "--info=progress2" in argv
    e_idx = argv.index("-e")
    assert "-F /tmp/cfg" in argv[e_idx + 1]


def test_rsync_progress_uses_progress2_when_supported(fake_run):
    s = make_session(fake_run)
    with patch("physai.ssh._rsync_supports_progress2", return_value=True):
        s.rsync("/src", "/dst", show_progress=True)
    argv = _last_argv(fake_run)
    assert "--info=progress2" in argv
    assert "--human-readable" in argv
    assert "--progress" not in argv


def test_rsync_progress_falls_back_to_progress_on_old_rsync(fake_run):
    """rsync < 3.1.0 / openrsync: use --progress, never --info=progress2."""
    s = make_session(fake_run)
    with patch("physai.ssh._rsync_supports_progress2", return_value=False):
        s.rsync("/src", "/dst", show_progress=True)
    argv = _last_argv(fake_run)
    assert "--progress" in argv
    assert "--info=progress2" not in argv
    assert not any(a.startswith("--info") for a in argv)


# ── rsync version capability probe ──────────────────────────────────────────


@pytest.mark.parametrize(
    "version_stdout, expected",
    [
        # Homebrew / modern Linux rsync
        ("rsync  version 3.4.1  protocol version 32", True),
        # Exactly the 3.1.0 cutoff where --info=progress2 was introduced
        ("rsync  version 3.1.0  protocol version 31", True),
        # macOS stock rsync — just below the cutoff
        ("rsync  version 3.0.9  protocol version 30", False),
        # openrsync (macOS 15+): "protocol version" line first (no dot after
        # "version", skipped), then a 2.6.9-compatible banner
        (
            "openrsync: protocol version 29\nrsync version 2.6.9 compatible",
            False,
        ),
        # Unparseable / empty output → conservative False
        ("", False),
        ("some unexpected banner", False),
    ],
)
def test_rsync_supports_progress2_parsing(version_stdout, expected):
    with patch("physai.ssh.subprocess.run") as run:
        run.return_value = MagicMock(returncode=0, stdout=version_stdout, stderr="")
        assert _rsync_supports_progress2() is expected


def test_rsync_supports_progress2_handles_missing_rsync():
    """No rsync on PATH → OSError → conservative False (caller uses --progress)."""
    with patch("physai.ssh.subprocess.run", side_effect=OSError("not found")):
        assert _rsync_supports_progress2() is False


# ── clone preserves ssh_config ──────────────────────────────────────────────


def test_clone_preserves_ssh_config(fake_run):
    s = make_session(fake_run, ssh_config="/tmp/cfg")
    c = s.clone()
    assert c.ssh_config == "/tmp/cfg"
    c.run("hostname")
    argv = _last_argv(fake_run)
    assert "-F" in argv and argv[argv.index("-F") + 1] == "/tmp/cfg"


# ── config.load wiring (both ways the value can be set) ─────────────────────


def test_load_ssh_config_from_file(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("host: filehost\nssh_config: /tmp/from-file\n")
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg_file)
    cfg = config_module.load()
    assert cfg["host"] == "filehost"
    assert cfg["ssh_config"] == "/tmp/from-file"


def test_load_ssh_config_expands_tilde(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("host: filehost\nssh_config: ~/somecfg\n")
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg_file)
    cfg = config_module.load()
    assert cfg["ssh_config"].endswith("/somecfg")
    assert "~" not in cfg["ssh_config"]


def test_load_ssh_config_override_beats_file(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("host: filehost\nssh_config: /tmp/from-file\n")
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg_file)
    cfg = config_module.load(ssh_config_override="/tmp/from-cli")
    assert cfg["ssh_config"] == "/tmp/from-cli"


def test_load_ssh_config_unset_is_none(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("host: filehost\n")
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg_file)
    cfg = config_module.load()
    assert cfg["ssh_config"] is None


def test_write_file_quotes_remote_path(fake_run):
    s = make_session(fake_run)
    s.write_file("/fsx/physai/sync/a b;id", "hello")
    argv = _last_argv(fake_run)
    assert argv[-1] == "cat > '/fsx/physai/sync/a b;id'"


def test_stream_log_quotes_job_id():
    with patch("physai.ssh.subprocess.Popen") as popen:
        popen.return_value.wait.return_value = 0
        s = object.__new__(Session)
        s.host = "myhost"
        s.ssh_config = None
        s._socket = "/tmp/sock"
        s.stream_log("1;id")
        argv = popen.call_args.args[0]
        assert argv[-1] == "python3 - '1;id'"
