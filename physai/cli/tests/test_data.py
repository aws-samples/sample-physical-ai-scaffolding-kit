"""Tests for physai data commands (ls, upload, rm)."""

from unittest.mock import MagicMock, patch

import pytest

from physai.data import ls, rm, upload


def test_ls_unknown_category():
    with pytest.raises(SystemExit, match="Unknown category"):
        ls(MagicMock(), "enroot")


def test_ls_empty(capsys):
    session = MagicMock()
    session.run.return_value = ""
    ls(session, "datasets")
    assert "(empty)" in capsys.readouterr().out


def test_ls_with_path(capsys):
    session = MagicMock()
    session.run.return_value = "4.0K\tfoo\n1.2G\tbar.hdf5"
    ls(session, "raw", "subdir")
    out = capsys.readouterr().out
    assert "foo" in out
    assert "bar.hdf5" in out
    assert "1.2G" in out
    # Sanity: command uses du -sh, not ls -l
    cmd = session.run.call_args[0][0]
    assert "du -sh" in cmd
    assert "/fsx/raw/subdir" in cmd


def test_upload_unknown_category():
    with pytest.raises(SystemExit, match="Unknown category"):
        upload(MagicMock(), "enroot", "/tmp")


def test_upload_missing_path(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        upload(MagicMock(), "datasets", str(tmp_path / "nope"))


def test_upload_dataset_rsyncs(tmp_path):
    d = tmp_path / "ds"
    d.mkdir()
    session = MagicMock()
    upload(session, "datasets", str(d))
    session.rsync.assert_called_once_with(
        str(d.resolve()), "/fsx/datasets/", show_progress=True
    )


def test_upload_raw_prompts_and_aborts(tmp_path, capsys):
    f = tmp_path / "x.hdf5"
    f.write_text("x")
    session = MagicMock()
    with patch("builtins.input", return_value="n"):
        upload(session, "raw", str(f))
    assert "Aborted." in capsys.readouterr().out
    session.rsync.assert_not_called()


def test_upload_raw_prompts_and_proceeds(tmp_path):
    f = tmp_path / "x.hdf5"
    f.write_text("x")
    session = MagicMock()
    with patch("builtins.input", return_value="y"):
        upload(session, "raw", str(f))
    session.rsync.assert_called_once_with(
        str(f.resolve()), "/fsx/raw/", show_progress=True
    )


# ── rm ──


def _mock_session_with_kind(kind: str, size: str = "1.2G", active: str | None = None):
    """Build a Session mock that answers kind-probe, size-probe, and rm calls."""
    session = MagicMock()

    def run_side_effect(cmd: str) -> str:
        if "-d " in cmd and "-f " in cmd:  # kind probe
            return kind
        if "du -sh" in cmd:
            return size + "\t/fsx/x"  # `cut -f1` only keeps the first field
        if cmd.startswith("squeue"):  # active-job probe
            if active:
                return f"{active}|physai/run/x/convert|produces=/fsx/datasets/foo/"
            return ""
        if cmd.startswith("rm -rf"):
            return ""
        raise AssertionError(f"unexpected session.run call: {cmd!r}")

    session.run.side_effect = run_side_effect
    return session


def test_rm_unknown_category():
    with pytest.raises(SystemExit, match="Unknown category"):
        rm(MagicMock(), "enroot", "foo")


def test_rm_rejects_slash_in_name():
    with pytest.raises(SystemExit, match="Invalid datasets name"):
        rm(MagicMock(), "datasets", "a/b")


def test_rm_rejects_dot_name():
    with pytest.raises(SystemExit, match="Invalid datasets name"):
        rm(MagicMock(), "datasets", "..")


def test_rm_missing_artifact():
    session = _mock_session_with_kind(kind="none")
    with pytest.raises(SystemExit, match="Nothing to remove"):
        rm(session, "datasets", "foo", force=True)


def test_rm_blocked_by_active_job():
    session = _mock_session_with_kind(kind="dir", active="123")
    with pytest.raises(SystemExit, match="Active job 123"):
        rm(session, "datasets", "foo", force=True)


def test_rm_prompts_and_aborts(capsys):
    session = _mock_session_with_kind(kind="dir")
    with patch("builtins.input", return_value="n"):
        rm(session, "datasets", "foo")
    out = capsys.readouterr().out
    assert "About to remove directory" in out
    assert "Aborted." in out
    # No rm -rf was issued
    for call in session.run.call_args_list:
        assert not call.args[0].startswith("rm -rf")


def test_rm_force_skips_prompt():
    session = _mock_session_with_kind(kind="dir")
    rm(session, "datasets", "foo", force=True)
    rm_calls = [c for c in session.run.call_args_list if c.args[0].startswith("rm -rf")]
    assert len(rm_calls) == 1
    assert "/fsx/datasets/foo" in rm_calls[0].args[0]


def test_rm_raw_shows_dra_warning(capsys):
    session = _mock_session_with_kind(kind="dir")
    with patch("builtins.input", return_value="n"):
        rm(session, "raw", "foo")
    assert "DRA cache" in capsys.readouterr().out


def test_rm_evaluations_category():
    """evaluations is in RM_CATEGORIES even though it's not in upload's CATEGORIES."""
    session = _mock_session_with_kind(kind="dir")
    rm(session, "evaluations", "run-2026", force=True)
    rm_calls = [c for c in session.run.call_args_list if c.args[0].startswith("rm -rf")]
    assert len(rm_calls) == 1
    assert "/fsx/evaluations/run-2026" in rm_calls[0].args[0]
