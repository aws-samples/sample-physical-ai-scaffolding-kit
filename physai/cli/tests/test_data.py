"""Tests for physai data commands (ls, upload)."""

from unittest.mock import MagicMock, patch

import pytest

from physai.data import ls, upload


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
    session.rsync.assert_called_once_with(str(d.resolve()), "/fsx/datasets/")


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
    session.rsync.assert_called_once_with(str(f.resolve()), "/fsx/raw/")
